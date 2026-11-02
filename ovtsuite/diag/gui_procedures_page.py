# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Procedures tab of a connected module, docs/OVT Diag Interface Options.pdf
page 3: on the left a gallery of procedure cards under category filters
(``All . 14`` ``Service`` ``Diagnosis`` ``TDI`` ``Gearbox`` ``Comfort &
coding`` ``Mine . 2``) with a dashed ``+ New procedure`` card; on the right the
selected procedure laid out as numbered step cards (read, choose, write ...),
its plan, and the run buttons.

A page of the window's module area, not a dialog. It runs nothing itself:
the window starts the engine (:class:`ovtsuite.diag.procedures.ProcedureRun`)
on the module's live session with the prompter this page hands out, so every
question the engine asks (``choose``, ``confirm``, ``show``, ``live``,
``wait``) appears in the step cards on the right. Qt only.

THE CONTRACT with :class:`ovtsuite.diag.gui_diag.DiagWindow` (keep it stable):

signals
    ``runRequested(dict)``         the procedure document the user chose to run
                                   (the window then calls :meth:`begin_run`)
    ``newProcedureRequested()``    the dashed ``+ New procedure`` card
    ``traceRequested()``           ``Show trace while writing``
methods
    ``set_procedures(docs, origins, applicable)``
        ``docs``: the procedure documents (``procedures.available``-shaped
        dicts with ``id, name, description, safety, applies, steps`` and an
        optional ``category``); ``origins``: ``{id: "bundled"|"module"|"user"}``
        (``user`` = the ``Mine`` filter); ``applicable``: ids that apply to the
        connected module (None = all); not applicable ones are listed dimmed
        with ``Not for this module``.
    ``set_context(addr, name, protocol, connected, logged_in, part="")``
        the module the procedures would run on (shown in the runner header:
        ``17 Instruments . 4F0910930 . KWP2000 over TP 2.0``; ``part`` is the
        optional part number); ``connected`` False disables Run with
        ``Connect to the module first``; the footer reads ``Login not needed
        on this module`` / ``Login required`` as the procedure's ``requires``
        says.
    ``begin_run(doc) -> Prompter``
        reset the right pane to the run of ``doc`` and return the
        :class:`procedures.Prompter` (a :class:`gui_procedure.GuiPrompter`
        compatible object) the window hands to ``ProcedureRun``.
    ``finish_run(result)``     a :class:`procedures.RunResult`: mark the steps done / failed,
                               show the writes with their verification, enable ``Run again``.
    ``is_running() -> bool`` and ``abort()``   (window close / disconnect: stops the prompter)
    ``select(procedure_id)``   show that procedure on the right (palette hits)

How the engine's questions land in the step cards: a document's steps become
cards -- ``read`` ``Read current values`` (or ``Read stored faults``), ``check``
``Check conditions``, ``live`` the step's title, ``choose`` its label, ``write``
``Write, verify, journal``; a ``show`` / ``wait`` / ``log`` step folds into the
neighbouring card. The engine's ``progress`` marks the card of the step it
begins as current (earlier ones done, collapsed to the values they showed); its
``choose`` opens as radio rows in that card, a ``confirm`` as the plan in a
mono box (with the ``Write N channels`` button), a ``show`` as a table, a
``live`` as a live table, a ``wait`` as a countdown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .gui_procedure import GuiPrompter, PromptView
from .gui_widgets import (Card, Chip, ClickableFrame, ElidedLabel, _Themed, mono_font, muted_label,
                          scaled_font, style_button, style_page, tokens)

Qt = QtCore.Qt

# --------------------------------------------------------------------- data
#: the document's ``safety`` string -> chip colour kind
SAFETY_KINDS = {"safe": "ok", "read-only": "ok", "conditions": "warn", "write": "warn", "coding": "warn",
                "actuates": "warn"}
CATEGORY_ORDER = ("Service", "Diagnosis", "TDI", "Gearbox", "Comfort & coding", "Other")
_SAFETY_TEXT = {
    "safe": "Safe to run: nothing is written.",
    "read-only": "Reads only; nothing is written.",
    "conditions": "Checks its conditions first; follow what it asks.",
    "write": "Writes to the module after your confirmation of the exact values; every write is journalled and re-read.",
    "coding": "Writes coding after your confirmation of the exact values; every write is journalled.",
    "actuates": "Actuates outputs on the module; follow the preconditions.",
}
#: standard VAG module addresses, for a card whose module is not the connected one
_ADDR_NAMES = {"01": "Engine", "02": "Transmission", "03": "Brakes", "08": "HVAC", "09": "Electrics",
               "15": "Airbags", "17": "Instruments", "19": "Gateway", "25": "Immobilizer",
               "44": "Steering assist", "46": "Comfort system", "52": "Door electronics", "56": "Radio",
               "5F": "Information electronics"}
_CH_RE = re.compile(r"^adaptation\.ch(\d+)$")
_EN = "–"
_DOT = "·"
_ARROW = "→"
JOURNAL_LINE = "Previous values are kept in the vehicle's journal so the change can be reverted."


def safety_of(doc: dict) -> str:
    return str(doc.get("safety") or "read-only")


def safety_chip(doc: dict) -> Tuple[str, str]:
    """``(text, kind)`` of a document's safety chip: the mapped kinds keep
    their word, an unknown string is shown as it is in neutral."""
    s = safety_of(doc)
    return s, SAFETY_KINDS.get(s, "neutral")


def _steps(doc: dict) -> List[Tuple[str, object]]:
    out = []
    for st in doc.get("steps") or []:
        if isinstance(st, dict) and st:
            k = next(iter(st))
            out.append((k, st[k]))
    return out


def write_targets(doc: dict) -> List[str]:
    """Entry ids the document's write steps write: named in the step or in
    the options of the ``choose`` its ``from`` takes them from."""
    ids: List[str] = []
    steps = _steps(doc)
    opts_of = {}
    for k, spec in steps:
        if k == "choose" and isinstance(spec, dict):
            opts_of[str(spec.get("id") or "choice")] = spec.get("options") or {}
    for k, spec in steps:
        if k != "write" or not isinstance(spec, dict):
            continue
        ids += [str(x) for x in (spec.get("writes") or {})]
        src = spec.get("from")
        if src is not None:
            for opt in (opts_of.get(str(src)) or {}).values():
                if isinstance(opt, dict):
                    ids += [str(x) for x in (opt.get("writes") or {})]
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def format_channels(nums: Sequence[int]) -> str:
    """``[2, 40, 41, 42]`` -> ``02/40-42`` (runs of three or more as a range)."""
    ns = sorted(set(int(n) for n in nums))
    parts, i = [], 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append("%02d%s%02d" % (ns[i], _EN, ns[j]))
        else:
            parts += ["%02d" % n for n in ns[i:j + 1]]
        i = j + 1
    return "/".join(parts)


def _entry_short(eid: str) -> str:
    m = _CH_RE.match(eid)
    return ("ch %02d" % int(m.group(1))) if m else eid.rsplit(".", 1)[-1]


def writes_text(ids: Sequence[str]) -> str:
    """What a list of entry ids is, briefly: ``adaptation ch 02/40-49``."""
    chans = [int(m.group(1)) for m in (_CH_RE.match(i) for i in ids) if m]
    others = [i for i in ids if not _CH_RE.match(i)]
    parts = []
    if chans:
        parts.append("adaptation ch %s" % format_channels(chans))
    parts += [o for o in others]
    return ", ".join(parts)


def touches(doc: dict) -> str:
    """What the procedure touches, for the card's second line."""
    ids = write_targets(doc)
    if ids:
        return writes_text(ids)
    parts: List[str] = []
    for k, spec in _steps(doc):
        if not isinstance(spec, dict):
            continue
        if k == "read":
            if spec.get("faults"):
                parts.append("faults")
            ch = [str(c) for c in spec.get("channels") or []]
            if ch:
                parts.append(writes_text(ch))
            g = spec.get("groups")
            if g in ("catalogue", "all"):
                parts.append("all measuring groups")
            elif g or spec.get("group") is not None:
                gl = g if isinstance(g, list) else [spec.get("group")]
                parts.append("group " + "/".join("%03d" % int(x) for x in gl if str(x).isdigit()))
        elif k == "live":
            g = spec.get("groups")
            gl = g if isinstance(g, list) else [spec.get("group")] if spec.get("group") is not None else []
            if gl:
                parts.append("live group " + "/".join("%03d" % int(x) for x in gl if str(x).isdigit()))
    out = []
    for p in parts:
        if p not in out:
            out.append(p)
    return ", ".join(out)


def category_of(doc: dict) -> str:
    """The document's ``category``, or one derived from what it does:
    ``Diagnosis`` read-only, ``Service`` adaptation writes only, ``Comfort &
    coding`` writes coding, else ``Other``."""
    c = doc.get("category")
    if isinstance(c, str) and c.strip():
        return c.strip()
    ids = write_targets(doc)
    s = safety_of(doc)
    if s == "coding" or any(i.startswith("coding") for i in ids):
        return "Comfort & coding"
    if not ids and s == "read-only":
        return "Diagnosis"
    if ids and all(i.startswith("adaptation") for i in ids):
        return "Service"
    return "Other"


def module_text(doc: dict, ctx_addr: str = "", ctx_name: str = "") -> str:
    a = (doc.get("applies") or {}).get("addr")
    if a is None or str(a) == "":
        return "Any module"
    addr = str(a).upper().zfill(2) if re.fullmatch(r"[0-9A-Fa-f]{1,2}", str(a)) else str(a)
    name = ctx_name if (ctx_addr and ctx_addr.upper().lstrip("0") == addr.lstrip("0")
                        and ctx_name) else _ADDR_NAMES.get(addr, "")
    return ("%s %s" % (addr, name)).strip()


def card_sub(doc: dict, ctx_addr: str = "", ctx_name: str = "") -> str:
    """``17 Instruments . adaptation ch 02/40-49 . 1 step``"""
    n = len(_steps(doc))
    parts = [module_text(doc, ctx_addr, ctx_name)]
    t = touches(doc)
    if t:
        parts.append(t)
    parts.append("%d step%s" % (n, "" if n == 1 else "s"))
    return (" %s " % _DOT).join(parts)


@dataclass
class CardSpec:
    """One step card: the engine steps it covers (``indices``, contiguous)."""
    kind: str                       # read | check | live | choose | write | show
    title: str
    sub: str = ""
    indices: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def first(self) -> int:
        return min(self.indices)

    @property
    def last(self) -> int:
        return max(self.indices)


def _humanize(name: str) -> str:
    return str(name).replace("_", " ")


def _primary_spec(kind: str, spec, i: int) -> CardSpec:
    d = spec if isinstance(spec, dict) else {}
    if kind == "read":
        faults, ch = bool(d.get("faults")), [str(c) for c in d.get("channels") or []]
        g = d.get("groups")
        has_groups = bool(g) or d.get("group") is not None
        bits = []
        if faults:
            bits.append("stored faults")
        if ch:
            bits.append(writes_text(ch))
        if g in ("catalogue", "all"):
            bits.append("every measuring group the module serves")
        elif has_groups:
            gl = g if isinstance(g, list) else [d.get("group")]
            bits.append("group " + ", ".join("%03d" % int(x) for x in gl if str(x).isdigit()))
        title = "Read stored faults" if (faults and not ch and not has_groups) else "Read current values"
        return CardSpec("read", title, ("Reads " + ", ".join(bits) + ".") if bits else "", [i])
    if kind == "check":
        items = spec if isinstance(spec, list) else [spec]
        names = []
        for it in items:
            if isinstance(it, dict):
                names += [_humanize(k) for k in it]
            else:
                names.append(_humanize(it))
        return CardSpec("check", "Check conditions", ("Needs: " + ", ".join(names) + ".") if names else "", [i])
    if kind == "live":
        g = d.get("groups")
        gl = g if isinstance(g, list) else [d.get("group")] if d.get("group") is not None else []
        title = str(d.get("title") or "Live readings")
        sub = str(d.get("text") or "")
        if not sub and gl:
            sub = "Group %s for %s s." % (", ".join("%03d" % int(x) for x in gl if str(x).isdigit()),
                                          ("%g" % float(d.get("seconds") or 15)))
        return CardSpec("live", title, sub, [i])
    if kind == "choose":
        opts = d.get("options") or {}
        labels = [str((v or {}).get("label", k)) if isinstance(v, dict) else str(v) for k, v in opts.items()]
        title = str(d.get("label") or d.get("text") or d.get("id") or "Choose")
        return CardSpec("choose", title, ("Options: " + " / ".join(labels)) if labels else "", [i])
    return CardSpec("write", "Write, verify, journal", JOURNAL_LINE, [i])


def procedure_cards(doc: dict) -> List[CardSpec]:
    """A document's steps as step cards. ``read`` ``check`` ``live`` ``choose``
    ``write`` open a card each; ``show`` ``wait`` ``log`` fold into a
    neighbour (a ``show`` with values and a ``wait`` / ``log`` into the one
    before, a plain ``show`` into the one after), so every card covers a
    contiguous range of the engine's steps."""
    cards: List[CardSpec] = []
    pending: List[Tuple[int, str, object]] = []         # folded forward, waiting for their card
    for i, (kind, spec) in enumerate(_steps(doc)):
        if kind in ("read", "check", "live", "choose", "write"):
            c = _primary_spec(kind, spec, i)
            for pi, pk, ps in pending:
                c.indices.insert(0, pi)
                _fold_note(c, pk, ps)
            pending = []
            cards.append(c)
            continue
        forward = pending or not cards or (kind == "show" and not (isinstance(spec, dict) and spec.get("values")))
        if forward:
            pending.append((i, kind, spec))
        else:
            cards[-1].indices.append(i)
            _fold_note(cards[-1], kind, spec)
    if pending:
        if cards:
            for pi, pk, ps in pending:
                cards[-1].indices.append(pi)
                _fold_note(cards[-1], pk, ps)
        else:
            c = CardSpec("show", "Show", "", [pi for pi, _k, _s in pending])
            for _pi, pk, ps in pending:
                _fold_note(c, pk, ps)
            c.sub = " ".join(c.notes)
            cards.append(c)
    for c in cards:
        c.indices.sort()
    return cards


def _fold_note(card: CardSpec, kind: str, spec) -> None:
    if kind == "log":
        note = spec.get("note") if isinstance(spec, dict) else ""
        if spec is True or (isinstance(spec, dict) and spec.get("journal", True)):
            card.notes.append("Kept in the journal%s." % ((": " + str(note)) if note else ""))
    elif kind == "wait":
        sec = float(spec.get("seconds") or 1) if isinstance(spec, dict) else float(spec or 1)
        card.notes.append("Waits %g s." % sec)
    elif kind == "show":
        text = spec if isinstance(spec, str) else str((spec or {}).get("text") or "")
        if text and not text.rstrip().endswith(":"):
            card.notes.append(text)


def parse_plan(detail: str) -> List[Tuple[str, str]]:
    """The engine's write confirmation (``channel 002 name: 150 -> 0``, one
    per line) as ``(label, change)`` pairs: ``("ch 02", "150 -> 0")``."""
    out = []
    for line in (detail or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, tail = line.rpartition(":")
        m = re.match(r"channel\s+(\d+)", head)
        if sep and m and "->" in tail:
            out.append(("ch %02d" % int(m.group(1)), tail.strip().replace("->", _ARROW)))
        else:
            out.append((line, ""))
    return out


def plan_text(items: Sequence[Tuple[str, str]], verified: bool = True) -> str:
    bits = [("%s: %s" % (a, b)) if b else a for a, b in items]
    n = len(bits)
    tail = "(%d adaptation write%s, each %s)" % (n, "" if n == 1 else "s",
                                                 "verified by re-read" if verified else "not re-read")
    return "will write: " + (" %s " % _DOT).join(bits) + "  " + tail


# ------------------------------------------------------------------ widgets
class _Flow(QtWidgets.QLayout):
    """Left to right, wrapping: the filter pills."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: List[QtWidgets.QLayoutItem] = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):                                    # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):                                        # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):                                        # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):                              # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):                                # noqa: N802
        return True

    def heightForWidth(self, w):                                # noqa: N802
        return self._lay(QtCore.QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):                                # noqa: N802
        super().setGeometry(rect)
        self._lay(rect, False)

    def sizeHint(self):                                         # noqa: N802
        return self.minimumSize()

    def minimumSize(self):                                      # noqa: N802
        s = QtCore.QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _lay(self, rect, test):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            sh = it.sizeHint()
            if x + sh.width() > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), sh))
            x += sh.width() + self._sp
            line_h = max(line_h, sh.height())
        return y + line_h - rect.y()


class _Pill(QtWidgets.QAbstractButton):
    """A category filter: a rounded pill, filled while it is the chosen one."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False

    def sizeHint(self):                                         # noqa: N802
        fm = QtGui.QFontMetrics(self.font())
        return QtCore.QSize(fm.horizontalAdvance(self.text()) + 26, fm.height() + 12)

    def enterEvent(self, ev):                                   # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):                                   # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def changeEvent(self, ev):                                  # noqa: N802
        if ev.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.ApplicationPaletteChange):
            self.update()
        super().changeEvent(ev)

    def paintEvent(self, _ev):                                  # noqa: N802
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            p.setBrush(t.accent_bg)
            p.setPen(QtGui.QPen(t.accent if not t.dark else t.accent_bg, 1))
            fg = t.text if t.dark else t.accent
        else:
            p.setBrush(t.card_hover if self._hover else Qt.BrushStyle.NoBrush)
            p.setPen(Qt.PenStyle.NoPen)
            fg = t.muted if not self._hover else t.text
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(self.font())
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())
        if self.hasFocus():
            p.setPen(QtGui.QPen(t.accent, 1, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), r.height() / 2 - 2, r.height() / 2 - 2)


class ClampLabel(QtWidgets.QWidget):
    """Wrapped text cut to ``lines`` lines with an ellipsis (a card's
    description): its height is always ``lines`` lines, so cards line up."""

    def __init__(self, text: str = "", lines: int = 2, parent=None, fixed: bool = True):
        super().__init__(parent)
        self._text, self._n, self._fixed = text, lines, fixed
        sp = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        sp.setHeightForWidth(not fixed)
        self.setSizePolicy(sp)
        self.setForegroundRole(QtGui.QPalette.ColorRole.WindowText)

    def hasHeightForWidth(self):                                # noqa: N802
        return not self._fixed

    def heightForWidth(self, w):                                # noqa: N802
        n = max(1, len(self.wrapped(w))) if self._text else 1
        return QtGui.QFontMetrics(self.font()).lineSpacing() * n + 2

    def text(self) -> str:
        return self._text

    def setText(self, text: str):                               # noqa: N802
        self._text = text
        self.update()

    def setLines(self, n: int):                                 # noqa: N802
        self._n = n
        self.updateGeometry()
        self.update()

    def sizeHint(self):                                         # noqa: N802
        return QtCore.QSize(120, QtGui.QFontMetrics(self.font()).lineSpacing() * self._n + 2)

    def minimumSizeHint(self):                                  # noqa: N802
        return QtCore.QSize(20, self.sizeHint().height())

    def wrapped(self, width: int) -> List[str]:
        fm = QtGui.QFontMetrics(self.font())
        words = self._text.split()
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if cur and fm.horizontalAdvance(trial) > width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        if len(lines) > self._n:
            rest = " ".join(lines[self._n - 1:])
            lines = lines[:self._n - 1] + [fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)]
        return lines

    def paintEvent(self, _ev):                                  # noqa: N802
        p = QtGui.QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))
        fm = QtGui.QFontMetrics(self.font())
        y = 0
        for line in self.wrapped(self.width()):
            p.drawText(0, y + fm.ascent() + 1, line)
            y += fm.lineSpacing()


class ProcedureCard(ClickableFrame):
    """One procedure of the gallery."""

    def __init__(self, doc: dict, origin: str, applicable: bool, sub: str, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.doc_id = str(doc.get("id", ""))
        self.origin = origin
        self.applicable = applicable
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 13, 16, 13)
        v.setSpacing(5)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.title = ElidedLabel(str(doc.get("name") or self.doc_id))
        self.title.setFont(scaled_font(self.title.font(), 1.05, True))
        top.addWidget(self.title, 1)
        if applicable:
            text, kind = safety_chip(doc)
        else:
            text, kind = "Not for this module", "neutral"
        self.chip = Chip(text, kind)
        top.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)
        self.sub = ElidedLabel(sub)
        self.sub.setFont(scaled_font(self.sub.font(), 0.94))
        self.sub.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        v.addWidget(self.sub)
        self.desc = ClampLabel(str(doc.get("description") or ""), 2)
        self.desc.setFont(scaled_font(self.desc.font(), 0.94))
        self.desc.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        v.addWidget(self.desc)
        if not applicable:
            eff = QtWidgets.QGraphicsOpacityEffect(self)
            eff.setOpacity(0.5)
            self.setGraphicsEffect(eff)
            self.setToolTip("Not for this module")
        self.setAccessibleName(self.title.fullText())

    def chip_text(self) -> str:
        return self.chip.text()

    def sub_text(self) -> str:
        return self.sub.fullText()

    def description(self) -> str:
        return self.desc.text()


class NewProcedureCard(ClickableFrame):
    """The dashed ``+ New procedure`` card."""
    TITLE = "+ New procedure"
    TEXT = "Write the steps in YAML in your local pack. Shareable as a file."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(122)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        self.setAccessibleName("New procedure")

    def sizeHint(self):                                         # noqa: N802
        return QtCore.QSize(200, 124)

    def paintEvent(self, _ev):                                  # noqa: N802
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(t.card_hover if self._hover else Qt.BrushStyle.NoBrush)
        pen = QtGui.QPen(t.accent if self.hasFocus() else t.border_strong, 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, 8, 8)
        inner = r.adjusted(18, 12, -18, -12)
        big = scaled_font(self.font(), 1.02)
        small = scaled_font(self.font(), 0.94)
        bfm, sfm = QtGui.QFontMetrics(big), QtGui.QFontMetrics(small)
        body = QtCore.QRectF(inner.left(), 0, inner.width(), 1000)
        opt = QtGui.QTextOption(Qt.AlignmentFlag.AlignHCenter)
        opt.setWrapMode(QtGui.QTextOption.WrapMode.WordWrap)
        bound = sfm.boundingRect(QtCore.QRect(0, 0, int(inner.width()), 1000),
                                 int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignHCenter), self.TEXT)
        total = bfm.height() + 8 + bound.height()
        y = inner.top() + (inner.height() - total) / 2
        p.setFont(big)
        p.setPen(t.accent)
        p.drawText(QtCore.QRectF(inner.left(), y, inner.width(), bfm.height()),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, self.TITLE)
        p.setFont(small)
        p.setPen(t.muted)
        p.drawText(QtCore.QRectF(inner.left(), y + bfm.height() + 8, inner.width(), bound.height() + 4),
                   int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignHCenter), self.TEXT)


class StepBadge(_Themed):
    """The numbered circle of a step card: pending (grey ring), current
    (accent ring), done (green with a check), failed (red cross), stopped."""

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.number = number
        self.state = "pending"
        self.setFixedSize(28, 28)

    def set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.update()

    def paintEvent(self, _ev):                                  # noqa: N802
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2)
        c = r.center()
        s = self.state
        white = QtGui.QColor(255, 255, 255) if not t.dark else QtGui.QColor(18, 24, 28)
        if s in ("done", "failed"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.ok if s == "done" else t.bad)
            p.drawEllipse(r)
            pen = QtGui.QPen(white, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            if s == "done":
                p.drawPolyline([QtCore.QPointF(c.x() - 4.2, c.y() + 0.2), QtCore.QPointF(c.x() - 1.2, c.y() + 3.4),
                                QtCore.QPointF(c.x() + 4.6, c.y() - 3.4)])
            else:
                p.drawLine(QtCore.QPointF(c.x() - 3.6, c.y() - 3.6), QtCore.QPointF(c.x() + 3.6, c.y() + 3.6))
                p.drawLine(QtCore.QPointF(c.x() - 3.6, c.y() + 3.6), QtCore.QPointF(c.x() + 3.6, c.y() - 3.6))
            return
        ring = {"current": t.accent, "stopped": t.warn}.get(s, t.border_strong)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(ring, 2.0 if s in ("current", "stopped") else 1.4))
        p.drawEllipse(r)
        p.setFont(scaled_font(self.font(), 0.92))
        p.setPen({"current": t.accent, "stopped": t.warn}.get(s, t.muted))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, "–" if s == "stopped" else str(self.number))


class PlanBox(_Themed):
    """The mono box of a write's plan (``will write: ch 02 -> 0 ...``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        self.label = QtWidgets.QLabel("")
        self.label.setWordWrap(True)
        self.label.setFont(mono_font(self.label.font(), 0.92))
        self.label.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label)
        self.setVisible(False)

    def text(self) -> str:
        return self.label.text()

    def set_plan(self, text: str):
        self.label.setText(text)
        self.setVisible(bool(text))

    def paintEvent(self, _ev):                                  # noqa: N802
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        fill = QtGui.QColor(t.window) if t.dark else QtGui.QColor(t.border)
        if not t.dark:
            fill.setAlpha(70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(QtCore.QRectF(self.rect()), 6, 6)


class _Pairs(QtWidgets.QWidget):
    """The values a done card showed: label / value pairs, two to a row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._g = QtWidgets.QGridLayout(self)
        self._g.setContentsMargins(0, 0, 0, 0)
        self._g.setHorizontalSpacing(14)
        self._g.setVerticalSpacing(3)
        self._g.setColumnStretch(1, 1)
        self._g.setColumnStretch(3, 1)
        self.pairs: List[Tuple[str, str]] = []

    def set_pairs(self, pairs: Sequence[Tuple[str, str]], more: int = 0):
        while self._g.count():
            it = self._g.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self.pairs = list(pairs)
        for i, (a, b) in enumerate(self.pairs):
            r, c = divmod(i, 2)
            la = QtWidgets.QLabel(a)
            la.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
            lb = QtWidgets.QLabel(b)
            lb.setFont(mono_font(lb.font(), 0.95))
            self._g.addWidget(la, r, c * 2)
            self._g.addWidget(lb, r, c * 2 + 1)
        if more > 0:
            lm = muted_label("+ %d more" % more)
            self._g.addWidget(lm, (len(self.pairs) + 1) // 2, 0, 1, 4)
        self.setVisible(bool(self.pairs))


class StepCard(Card):
    """One numbered step of the procedure: its badge, title and, by state, a
    description (pending), the engine's question (current) or the values it
    showed (done)."""

    def __init__(self, spec: CardSpec, number: int, parent=None):
        super().__init__(parent, radius=9)
        self.spec = spec
        self.number = number
        self.state = "pending"
        self.values: List[List[str]] = []
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(14, 12, 16, 12)
        h.setSpacing(12)
        self.badge = StepBadge(number)
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(5)
        self.title = QtWidgets.QLabel(spec.title)
        self.title.setWordWrap(True)
        self._title_font = QtGui.QFont(self.title.font())
        col.addWidget(self.title)
        sub = " ".join([x for x in [spec.sub] + spec.notes if x])
        self.sub = ClampLabel(sub, 3, fixed=False)
        self.sub.setFont(scaled_font(self.sub.font(), 0.94))
        self.sub.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.sub.setVisible(bool(sub))
        col.addWidget(self.sub)
        self.pairs = _Pairs()
        self.pairs.setVisible(False)
        col.addWidget(self.pairs)
        self.slot = QtWidgets.QVBoxLayout()
        self.slot.setContentsMargins(0, 2, 0, 0)
        col.addLayout(self.slot)
        self.plan = PlanBox()
        col.addWidget(self.plan)
        h.addLayout(col, 1)
        self._has_prompt = False
        self.set_state("pending")

    def set_state(self, state: str):
        self.state = state
        self.badge.set_state(state)
        f = scaled_font(self._title_font, 1.02, state in ("current", "done", "failed", "stopped"))
        self.title.setFont(f)
        self.title.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText if state == "pending"
                                     else QtGui.QPalette.ColorRole.WindowText)
        self.setEdge({"current": "accent", "failed": "bad", "stopped": "warn"}.get(state, ""))
        self._sync()

    def set_choice(self, label: str):
        """What was chosen, in place of the list of options."""
        self.sub.setText("Chosen: %s" % label)

    def set_prompt_active(self, on: bool):
        self._has_prompt = on
        self._sync()

    def set_values(self, rows: Sequence[Sequence]):
        self.values = [list(r) for r in rows or []]
        pairs = []
        for r in self.values:
            label = str(r[1]) if len(r) > 1 and r[1] else (str(r[0]) if r else "")
            val = str(r[2]) if len(r) > 2 else ""
            pairs.append((label, val))
        shown = pairs[:6]
        self.pairs.set_pairs(shown, len(pairs) - len(shown))
        self._sync()

    def _sync(self):
        done = self.state in ("done", "failed", "stopped")
        collapsed_values = done and bool(self.values) and not self._has_prompt
        self.pairs.setVisible(collapsed_values)
        self.sub.setVisible(bool(self.sub.text()) and not self._has_prompt and not collapsed_values
                            and self.state != "current")
        self.sub.setLines(3 if self.state == "pending" else 2)


class PlanSummary(Card):
    """Before a run: what the selected procedure is and does."""

    def __init__(self, doc: dict, parent=None):
        super().__init__(parent, radius=9)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 13, 16, 13)
        v.setSpacing(8)
        text = str(doc.get("description") or "")
        if text:
            d = QtWidgets.QLabel(text)
            d.setWordWrap(True)
            v.addWidget(d)
        s_text, s_kind = safety_chip(doc)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.chip = Chip(s_text, s_kind)
        row.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        self.safety_line = QtWidgets.QLabel(_SAFETY_TEXT.get(s_text, s_text))
        self.safety_line.setWordWrap(True)
        self.safety_line.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        row.addWidget(self.safety_line, 1)
        v.addLayout(row)
        ids = write_targets(doc)
        self.writes_line = QtWidgets.QLabel(("Writes %s." % writes_text(ids)) if ids else "Writes nothing.")
        self.writes_line.setWordWrap(True)
        self.writes_line.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        v.addWidget(self.writes_line)


class ResultCard(Card):
    """After a run: how it ended, the writes with their verification, notes."""

    def __init__(self, result, parent=None):
        super().__init__(parent, radius=9)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 13, 16, 13)
        v.setSpacing(7)
        st = str(getattr(result, "status", "done"))
        words = {"done": ("Done", "ok"), "cancelled": ("Stopped", "warn"), "failed": ("Failed", "bad"),
                 "blocked": ("Blocked", "bad")}
        text, kind = words.get(st, (st, "neutral"))
        head = QtWidgets.QHBoxLayout()
        self.chip = Chip(text, kind)
        head.addWidget(self.chip)
        msg = str(getattr(result, "message", "") or "")
        self.message = QtWidgets.QLabel(msg)
        self.message.setWordWrap(True)
        head.addWidget(self.message, 1)
        v.addLayout(head)
        self.write_rows: List[Tuple[QtWidgets.QLabel, Chip]] = []
        for w in getattr(result, "writes", None) or []:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(10)
            ch = ("ch %02d" % w.channel) if isinstance(getattr(w, "channel", None), int) else str(w.entry_id)
            lab = QtWidgets.QLabel("%s   %s %s %s" % (ch, "-" if w.before is None else w.before, _ARROW, w.wanted))
            lab.setFont(mono_font(lab.font(), 0.95))
            row.addWidget(lab)
            row.addStretch(1)
            ver = w.verified
            chip = Chip("verified" if ver else ("not verified" if ver is False else "not re-read"),
                        "ok" if ver else ("bad" if ver is False else "neutral"))
            row.addWidget(chip)
            v.addLayout(row)
            self.write_rows.append((lab, chip))
        self.notes = []
        for n in getattr(result, "notes", None) or []:
            lab = QtWidgets.QLabel(str(n))
            lab.setWordWrap(True)
            lab.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
            v.addWidget(lab)
            self.notes.append(lab)
        if not self.write_rows and st == "done":
            none = QtWidgets.QLabel("Nothing was written.")
            none.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
            v.addWidget(none)


class _VLine(_Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(1)

    def paintEvent(self, _ev):                                  # noqa: N802
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), tokens(self.palette()).border)


class _GridHost(QtWidgets.QWidget):
    resized = QtCore.Signal()

    def resizeEvent(self, ev):                                  # noqa: N802
        super().resizeEvent(ev)
        self.resized.emit()


def _scroll(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    sa = QtWidgets.QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(widget)
    sa.viewport().setAutoFillBackground(False)
    widget.setAutoFillBackground(False)
    return sa


# --------------------------------------------------------------------- page
class ProceduresPage(QtWidgets.QWidget):
    runRequested = QtCore.Signal(dict)
    newProcedureRequested = QtCore.Signal()
    traceRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        style_page(self, "procPage")
        self._docs: List[dict] = []
        self._origins: Dict[str, str] = {}
        self._applicable: Optional[set] = None
        self._filter = "All"
        self._selected: Optional[str] = None
        self._ctx = {"addr": "", "name": "", "protocol": "", "part": "", "connected": False, "logged_in": False}
        self._cards: Dict[str, ProcedureCard] = {}
        self._new_card: Optional[NewProcedureCard] = None
        self._pills: List[_Pill] = []
        self._cols = 2
        # run state
        self._state = "idle"                   # idle | running | done
        self._prompter: Optional[GuiPrompter] = None
        self._run_doc: Optional[dict] = None
        self._specs: List[CardSpec] = []
        self.step_cards: List[StepCard] = []
        self._cur = -1                         # the engine step in progress
        self._mode = ""                        # what the engine waits for: choose/confirm/show/live/wait
        self._stopping = False
        self._result = None
        self._extra: List[QtWidgets.QWidget] = []       # the plan summary / result card
        self._primary_action = ""
        self._plan_n = 0

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_left(), 11)
        root.addWidget(_VLine())
        root.addWidget(self._build_right(), 9)
        self._refresh_all()

    # ------------------------------------------------------------ building
    def _build_left(self) -> QtWidgets.QWidget:
        left = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(left)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)
        self._pill_host = QtWidgets.QWidget()
        self._pill_flow = _Flow(self._pill_host, 6)
        v.addWidget(self._pill_host)
        self._grid_host = _GridHost()
        self._grid = QtWidgets.QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        self._grid_host.resized.connect(self._relayout_grid)
        self._left_scroll = _scroll(self._grid_host)
        v.addWidget(self._left_scroll, 1)
        return left

    def _build_right(self) -> QtWidgets.QWidget:
        right = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(right)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        self.title_label = QtWidgets.QLabel("")
        self.title_label.setFont(scaled_font(self.title_label.font(), 1.2, True))
        self.title_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Preferred)
        head.addWidget(self.title_label, 0)
        self.context_label = ElidedLabel("")
        self.context_label.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        head.addWidget(self.context_label, 1)
        self.stop_btn = style_button(QtWidgets.QPushButton("Stop"), "danger")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)
        head.addWidget(self.stop_btn, 0)
        v.addLayout(head)
        self._steps_host = QtWidgets.QWidget()
        self._steps_l = QtWidgets.QVBoxLayout(self._steps_host)
        self._steps_l.setContentsMargins(0, 0, 0, 0)
        self._steps_l.setSpacing(10)
        self._empty = muted_label("Select a procedure on the left to see its steps.")
        self._steps_l.addWidget(self._empty)
        self._steps_l.addStretch(1)
        self._steps_scroll = _scroll(self._steps_host)
        v.addWidget(self._steps_scroll, 1)
        # the prompt of the engine's current question, moved into the current card
        self._prompt = PromptView(compact=True)
        self._prompt.selectionChanged.connect(self._on_choice_changed)
        self._prompt.setVisible(False)
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        self.run_btn = style_button(QtWidgets.QPushButton("Run"), "primary")
        self.run_btn.clicked.connect(self._on_primary)
        bar.addWidget(self.run_btn)
        self.alt_btn = style_button(QtWidgets.QPushButton("No"), "outline")
        self.alt_btn.clicked.connect(self._on_alt)
        bar.addWidget(self.alt_btn)
        self.trace_btn = style_button(QtWidgets.QPushButton("Show trace while writing"), "outline")
        self.trace_btn.clicked.connect(self.traceRequested)
        bar.addWidget(self.trace_btn)
        self.login_label = ElidedLabel("")
        self.login_label.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.login_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        bar.addWidget(self.login_label, 1)
        v.addLayout(bar)
        return right

    # ----------------------------------------------------------- contract
    def set_procedures(self, docs: Sequence[dict], origins: Dict[str, str],
                       applicable: Optional[set] = None) -> None:
        self._docs = [d for d in docs if isinstance(d, dict)]
        self._origins = dict(origins or {})
        self._applicable = None if applicable is None else set(applicable)
        if self._filter not in self.filter_names():
            self._filter = "All"
        keep = self._selected if any(str(d.get("id")) == self._selected for d in self._docs) else None
        self._selected = keep
        self._rebuild_gallery()
        if self._state == "idle":
            self._show_selected()
        self._refresh_chrome()

    def set_context(self, addr: str, name: str, protocol: str, connected: bool, logged_in: bool,
                    part: str = "") -> None:
        self._ctx = {"addr": str(addr or ""), "name": str(name or ""), "protocol": str(protocol or ""),
                     "part": str(part or ""), "connected": bool(connected), "logged_in": bool(logged_in)}
        for c in self._cards.values():
            c.sub.setText(card_sub(c.doc, self._ctx["addr"], self._ctx["name"]))
        self._refresh_chrome()

    def begin_run(self, doc: dict):
        self._drop_prompter()
        self._run_doc = doc
        if str(doc.get("id", "")) in self._cards:
            self._selected = str(doc.get("id"))
            self._mark_selected()
        self._prompter = GuiPrompter()
        self._prompter.ask.connect(self._on_ask)
        self._state = "running"
        self._cur, self._mode, self._stopping, self._result = -1, "", False, None
        self._build_steps(doc)
        self._refresh_chrome()
        return self._prompter

    def finish_run(self, result) -> None:
        if self._run_doc is None or not self.step_cards:
            doc = next((d for d in self._docs if str(d.get("id")) == str(getattr(result, "procedure_id", ""))), None)
            if doc is not None:
                self._run_doc = doc
                self._build_steps(doc)
        self._result = result
        self._state = "done"
        self._mode = ""
        self._stopping = False
        self._detach_prompt()
        st = str(getattr(result, "status", "done"))
        cur = self._card_of_step(self._cur)
        for i, c in enumerate(self.step_cards):
            if st == "done":
                c.set_state("done")
            elif cur is None:
                c.set_state("pending")
            elif i < cur:
                c.set_state("done")
            elif i == cur:
                c.set_state("failed" if st in ("failed", "blocked") else "stopped")
            else:
                c.set_state("pending")
        self._clear_extra()
        card = ResultCard(result)
        self._extra.append(card)
        self._steps_l.insertWidget(self._steps_l.count() - 1, card)
        self._refresh_chrome()
        QtCore.QTimer.singleShot(0, lambda: self._steps_scroll.ensureWidgetVisible(card))

    def is_running(self) -> bool:
        return self._state == "running"

    def abort(self) -> None:
        """Stops a waiting prompter: the engine blocked on a question (a
        choice, a confirmation, a live step) is released and cancels."""
        p = self._prompter
        if p is not None and self._state == "running":
            p.stop()

    def select(self, procedure_id: str) -> None:
        pid = str(procedure_id)
        if self._state == "running" or pid not in self._cards:
            return
        if self._filter != "All" and not self._matches(self._cards[pid].doc, self._filter):
            self.set_filter("All")
        self._selected = pid
        self._mark_selected()
        self._state = "idle"
        self._show_selected()
        self._refresh_chrome()
        QtCore.QTimer.singleShot(0, lambda: self._left_scroll.ensureWidgetVisible(self._cards[pid]))

    # --------------------------------------------------------- the gallery
    def filter_names(self) -> List[str]:
        cats = []
        for d in self._docs:
            c = category_of(d)
            if c not in cats:
                cats.append(c)
        ordered = [c for c in CATEGORY_ORDER if c in cats] + sorted(c for c in cats if c not in CATEGORY_ORDER)
        return ["All"] + ordered + ["Mine"]

    def _matches(self, doc: dict, flt: str) -> bool:
        if flt == "All":
            return True
        if flt == "Mine":
            return self._origins.get(str(doc.get("id")), "") == "user"
        return category_of(doc) == flt

    def count(self, flt: str) -> int:
        return sum(1 for d in self._docs if self._matches(d, flt))

    def pill_texts(self) -> List[str]:
        return [p.text() for p in self._pills]

    def set_filter(self, name: str) -> None:
        if name not in self.filter_names():
            return
        self._filter = name
        for p in self._pills:
            p.setChecked(p.property("flt") == name)
        self._relayout_grid(force=True)

    def current_filter(self) -> str:
        return self._filter

    def visible_ids(self) -> List[str]:
        return [str(d.get("id")) for d in self._docs if self._matches(d, self._filter)]

    def card(self, procedure_id: str) -> Optional[ProcedureCard]:
        return self._cards.get(str(procedure_id))

    def new_card(self) -> Optional[NewProcedureCard]:
        return self._new_card

    def selected_id(self) -> Optional[str]:
        return self._selected

    def _is_applicable(self, doc: dict) -> bool:
        return self._applicable is None or str(doc.get("id")) in self._applicable

    def _rebuild_gallery(self):
        # pills
        for p in self._pills:
            self._pill_flow.removeWidget(p)
            p.hide()
            p.setParent(None)
            p.deleteLater()
        self._pills = []
        for name in self.filter_names():
            n = self.count(name)
            text = "%s %s %d" % (name, _DOT, n) if name in ("All", "Mine") else name
            p = _Pill(text)
            p.setProperty("flt", name)
            p.setChecked(name == self._filter)
            if name not in ("All", "Mine"):
                p.setToolTip("%d procedure%s" % (n, "" if n == 1 else "s"))
            p.clicked.connect(lambda _c=False, nm=name: self.set_filter(nm))
            self._pill_flow.addWidget(p)
            self._pills.append(p)
        self._pill_host.updateGeometry()
        # cards
        for c in list(self._cards.values()) + ([self._new_card] if self._new_card is not None else []):
            c.hide()
            c.setParent(None)
            c.deleteLater()
        self._cards = {}
        for d in self._docs:
            pid = str(d.get("id", ""))
            c = ProcedureCard(d, self._origins.get(pid, ""), self._is_applicable(d),
                              card_sub(d, self._ctx["addr"], self._ctx["name"]), self._grid_host)
            c.clicked.connect(lambda pid=pid: self._on_card_clicked(pid))
            self._cards[pid] = c
        self._new_card = NewProcedureCard(self._grid_host)
        self._new_card.clicked.connect(self.newProcedureRequested)
        self._mark_selected()
        self._relayout_grid(force=True)

    def _relayout_grid(self, force: bool = False):
        w = self._left_scroll.viewport().width()
        cols = 2 if w >= 440 else 1
        if cols == self._cols and not force:
            return
        self._cols = cols
        g = self._grid
        while g.count():
            g.takeAt(0)
        for c in self._cards.values():
            c.setVisible(False)
        i = 0
        for d in self._docs:
            if not self._matches(d, self._filter):
                continue
            c = self._cards[str(d.get("id", ""))]
            g.addWidget(c, i // cols, i % cols)
            c.setVisible(True)
            i += 1
        if self._new_card is not None:
            g.addWidget(self._new_card, i // cols, i % cols)
            self._new_card.setVisible(True)
            i += 1
        for r in range(g.rowCount() + 1):
            g.setRowStretch(r, 0)
        for c in range(2):
            g.setColumnStretch(c, 1 if c < cols else 0)
        g.setRowStretch(i // cols + 1, 1)

    def _mark_selected(self):
        for pid, c in self._cards.items():
            c.setSelected(pid == self._selected)

    def _on_card_clicked(self, pid: str):
        if self._state == "running":
            return
        self._selected = pid
        self._mark_selected()
        self._state = "idle"
        self._show_selected()
        self._refresh_chrome()

    # ------------------------------------------------------------ right pane
    def _doc(self, pid: Optional[str]) -> Optional[dict]:
        return next((d for d in self._docs if str(d.get("id")) == pid), None) if pid else None

    def _current_doc(self) -> Optional[dict]:
        if self._state in ("running", "done") and self._run_doc is not None:
            return self._run_doc
        return self._doc(self._selected)

    def _clear_extra(self):
        for w in self._extra:
            self._steps_l.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._extra = []

    def _clear_steps(self):
        self._detach_prompt()
        self._clear_extra()
        for c in self.step_cards:
            self._steps_l.removeWidget(c)
            c.hide()
            c.setParent(None)
            c.deleteLater()
        self.step_cards = []
        self._specs = []

    def _show_selected(self):
        doc = self._doc(self._selected)
        self._build_steps(doc, plan=True)

    def _build_steps(self, doc: Optional[dict], plan: bool = False):
        self._clear_steps()
        self._empty.setVisible(doc is None)
        if doc is None:
            return
        pos = 0
        if plan:
            s = PlanSummary(doc)
            self._extra.append(s)
            self._steps_l.insertWidget(pos, s)
            pos += 1
        self._specs = procedure_cards(doc)
        for i, sp in enumerate(self._specs):
            c = StepCard(sp, i + 1)
            self.step_cards.append(c)
            self._steps_l.insertWidget(pos + i, c)

    def _card_of_step(self, step: int) -> Optional[int]:
        if step < 0:
            return None
        for i, sp in enumerate(self._specs):
            if sp.first <= step <= sp.last:
                return i
        return None

    def _step_spec(self, step: int):
        """``(kind, spec)`` of the engine's step ``step`` of the running document."""
        st = _steps(self._run_doc or {})
        return st[step] if 0 <= step < len(st) else ("", None)

    def _apply_states(self):
        cur = self._card_of_step(self._cur)
        for i, c in enumerate(self.step_cards):
            if cur is None:
                c.set_state("pending")
            elif i < cur:
                c.set_state("done")
            elif i == cur:
                c.set_state("current")
            else:
                c.set_state("pending")

    # ---------------------------------------------------------- the prompt
    def _detach_prompt(self):
        for c in self.step_cards:
            if c.slot.indexOf(self._prompt) >= 0:
                c.slot.removeWidget(self._prompt)
                c.set_prompt_active(False)
        self._prompt.setVisible(False)
        self._prompt.setParent(self)
        self._prompt.setVisible(False)
        for c in self.step_cards:
            c.plan.set_plan("")

    def _attach_prompt(self, idx: int):
        card = self.step_cards[idx]
        if card.slot.indexOf(self._prompt) < 0:
            self._detach_prompt()
            card.slot.addWidget(self._prompt)
        self._prompt.setVisible(True)
        card.set_prompt_active(True)
        return card

    def _on_ask(self, kind: str, payload):
        if self._state != "running":
            return
        p = payload or {}
        if kind == "progress":
            self._cur = int(p.get("index", 0))
            self._mode = ""
            self._detach_prompt()
            self._apply_states()
            self._refresh_chrome()
            idx = self._card_of_step(self._cur)
            if idx is not None:
                card = self.step_cards[idx]
                QtCore.QTimer.singleShot(0, lambda: self._steps_scroll.ensureWidgetVisible(card))
            return
        if not self.step_cards:
            return
        idx = self._card_of_step(self._cur)
        if idx is None:
            idx = 0
            if self._cur < 0:
                self._cur = self._specs[0].first
                self._apply_states()
        card = self._attach_prompt(idx)
        self._prompt.setEnabled(True)
        mode = self._prompt.apply(kind, p)
        if kind == "choose":
            self._prompt.message.setVisible(False)          # the card's title is the question
        if kind == "confirm":
            self._prompt.detail.setVisible(False)
            items = parse_plan(p.get("detail", ""))
            self._plan_n = len(items)
            sk, ss = self._step_spec(self._cur)
            verified = not (isinstance(ss, dict) and str(ss.get("verify") or "reread") == "none")
            card.plan.set_plan(plan_text(items, verified) if items else "")
        elif kind == "choose":
            self._on_choice_changed()
        if kind in ("show", "live_rows"):
            card.set_values(p.get("rows") or [])
        if mode:
            self._mode = "" if mode == "idle" else mode
        self._refresh_chrome()
        QtCore.QTimer.singleShot(0, lambda: self._steps_scroll.ensureWidgetVisible(card))

    def _on_choice_changed(self):
        """The ``will write`` preview under a choice's options."""
        if self._state != "running" or self._prompt.mode != "choose":
            return
        idx = self._card_of_step(self._cur)
        if idx is None:
            return
        kind, spec = self._step_spec(self._cur)
        card = self.step_cards[idx]
        key = self._prompt.selected_key()
        opt = ((spec or {}).get("options") or {}).get(key) if isinstance(spec, dict) else None
        writes = (opt or {}).get("writes") if isinstance(opt, dict) else None
        if writes:
            items = [(_entry_short(str(k)), "%s %s" % (_ARROW, v)) for k, v in writes.items()]
            n = len(items)
            card.plan.set_plan("will write: " + (" %s " % _DOT).join("%s %s" % a for a in items)
                               + "  (%d adaptation write%s, each verified by re-read)" % (n, "" if n == 1 else "s"))
        else:
            card.plan.set_plan("")
        self._refresh_chrome()

    # -------------------------------------------------------------- buttons
    def _on_primary(self):
        a = self._primary_action
        if a in ("run", "again"):
            doc = self._current_doc()
            if doc is not None:
                self.runRequested.emit(doc)
            return
        self._answer_with(a)

    def _on_alt(self):
        if self._primary_action in ("write", "yes"):
            self._answer(False)

    def _answer_with(self, a: str):
        if self._prompter is None or self._state != "running":
            return
        if a == "choose":
            key = self._prompt.selected_key()
            if key is None:
                return
            idx = self._card_of_step(self._cur)
            if idx is not None:
                radio = next((r for r in self._prompt._radios if r.property("key") == key), None)
                self.step_cards[idx].set_choice(radio.text() if radio is not None else str(key))
            self._answer(key)
        elif a in ("show", "write", "yes"):
            self._answer(True)
        elif a == "skip":
            self._prompter.answer("skip")

    def _answer(self, value):
        if self._prompter is None:
            return
        self._mode = ""
        self._prompt.setEnabled(False)
        self._prompter.answer(value)
        self._refresh_chrome()

    def _on_stop(self):
        if self._prompter is None or self._state != "running":
            return
        self._stopping = True
        self._prompter.stop()
        self._prompt.setEnabled(False)
        self._mode = ""
        self._refresh_chrome()

    def _drop_prompter(self):
        p = self._prompter
        if p is not None:
            try:
                p.ask.disconnect(self._on_ask)
            except (RuntimeError, TypeError):
                pass
            if self._state == "running":
                p.stop()
        self._prompter = None

    # --------------------------------------------------------------- chrome
    def _login_text(self, doc: Optional[dict]) -> str:
        if doc is None:
            return ""
        req = doc.get("requires")
        flat = (" ".join(map(str, req)) if isinstance(req, (list, tuple, set)) else
                " ".join("%s %s" % kv for kv in req.items()) if isinstance(req, dict) else str(req or "")).lower()
        needs = True if "login" in flat else None
        if needs is None and write_targets(doc):
            return "Logged in" if self._ctx["logged_in"] else "Login may be needed to write"
        if needs:
            return "Logged in" if self._ctx["logged_in"] else "Login required"
        return "Login not needed on this module"

    def _refresh_all(self):
        self._rebuild_gallery()
        self._refresh_chrome()

    def _refresh_chrome(self):
        doc = self._current_doc()
        c = self._ctx
        self.title_label.setText(str(doc.get("name") or doc.get("id")) if doc else "")
        bits = [("%s %s" % (c["addr"], c["name"])).strip(), c["part"], c["protocol"]]
        self.context_label.setText((" %s " % _DOT).join(b for b in bits if b))
        self.login_label.setText(self._login_text(doc))
        self.login_label.setVisible(doc is not None)
        self.stop_btn.setVisible(self._state == "running")
        self.stop_btn.setEnabled(self._state == "running" and not self._stopping)
        self.stop_btn.setText("Stopping..." if self._stopping else "Stop")
        alt = False
        act, text, enabled = "", "Run", False
        if self._state == "running":
            m = self._mode
            if self._stopping or not m:
                act, text, enabled = "", "Working...", False
            elif m == "choose":
                act, text, enabled = "choose", "Continue", self._prompt.selected_key() is not None
            elif m == "show":
                act, text, enabled = "show", "Continue", True
            elif m == "confirm":
                kind, _spec = self._step_spec(self._cur)
                if kind == "write":
                    n = self._plan_n
                    act, text = "write", ("Write %d channel%s" % (n, "" if n == 1 else "s")) if n else "Write"
                    self.alt_btn.setText("Don't write")
                else:
                    act, text = "yes", "Yes"
                    self.alt_btn.setText("No")
                enabled, alt = True, True
            elif m in ("live", "wait"):
                act, text, enabled = "skip", "Skip the rest of this step", True
        else:
            act = "again" if self._state == "done" else "run"
            text = "Run again" if self._state == "done" else "Run"
            if doc is None:
                enabled = False
            elif not self._ctx["connected"]:
                text, enabled = "Connect to the module first", False
            elif not self._is_applicable(doc):
                text, enabled = "Not for this module", False
            else:
                enabled = True
        self._primary_action = act
        self.run_btn.setText(text)
        self.run_btn.setEnabled(enabled)
        self.alt_btn.setVisible(alt)
        self.trace_btn.setVisible(doc is not None)


__all__ = ["ProceduresPage", "ProcedureCard", "NewProcedureCard", "StepCard", "CardSpec", "procedure_cards",
           "category_of", "safety_chip", "card_sub", "write_targets", "parse_plan", "format_channels"]
