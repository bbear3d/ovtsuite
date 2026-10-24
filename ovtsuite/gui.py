# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""PySide6 GUI: primary + comparison sessions, tier tree, unit editing.

The window is a view over one or two TunerSessions. The left tree groups
entities by tuning tier, then A2L category; the center is a spreadsheet-like
editor in physical units with undo/redo; a comparison binary (which may have
a different internal structure) is paired by KB identity, not by address, so
cell-level differences highlight correctly across calibrations.

Importing this module does not require a display; constructing MainWindow
does. Where PySide6 is missing, ``HAVE_QT`` is False and the CLI still runs.
"""

from typing import Dict, List, Optional

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    HAVE_QT = True
except Exception:                                   # pragma: no cover
    HAVE_QT = False

from . import branding
from .appsettings import settings as _settings
# Speed-map views: the base app's generic ones, or the ZF 6HP module's when
# it is installed (ovtsuite.speedview dispatches).
from . import speedview as _view6
from . import platforms as _platforms
from .modules import registry as _registry


def _dtc6():
    """The ZF 6HP fault-code module (only reached from a 6HP session, whose
    module is therefore installed)."""
    from .zf6hp import dtc6hp
    return dtc6hp
from . import progress as _progress
from . import gui_search as _gui_search
from . import gui_ai as _gui_ai
from . import gui_project as _gp
from .session import TunerSession
from .tiers import TIER_NAMES, tier_name
from . import catalogue as _catalogue
from .groups import GROUP_HELP as _GROUP_HELP, ADVANCED as _ADVANCED_GROUPS
from .groups import is_advanced as _is_advanced
from .model import Entity
from .transfer import build_overlay
from .diff import same_extent as _same_extent, cells_differ as _cells_differ
from .diff import axis_changes as _axis_changes
from .msblock import MeasuringBlocks
from .gears import GearRatios
from .variant import vss_pulse_settings
from .signals import SignalCatalog, observed_ids, EMPTY_IDS
from functools import lru_cache as _lru_cache
from .signal_names import translate_german as _tr_de

import re as _re0

# a leading provenance-tag segment like "A2L: Sym_Bol" or "OLS: ACC_x_CUR"
# (the MF/HQ Audi XDFs prefix every description this way); the real English
# text is the segment after it.
#: Floor for a map column's width, in pixels. Qt sizes a column to its
#: contents, which on a table of short integers leaves it narrower than the
#: value it will hold once the Z mode is switched to engine rpm or km/h. Wide
#: enough for "-6710.4" in the default font.
_MIN_CELL_W = 64

#: Rows sampled when sizing a column to its contents. Qt's default is every
#: row, which on the widest tables is a six-figure number of ``data()`` calls
#: per column on every selection change.
_FIT_SAMPLE_ROWS = 200

#: How long after the last edit the status line verifies the checksums.
#: Verifying sums the whole image (~0.2 s on an EDC16), too slow per cell.
CHECKSUM_DELAY_MS = 400

_TAG_LEAD = _re0.compile(
    r'^[A-Za-z0-9]{2,5}:\s*[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9#]+)*$')


def _parse_desc(e):
    """Split the A2L long description into (main, axes) where main is the plain
    English description and axes maps 'X'/'Y'/'Z' to their axis descriptions.
    The A2L text is '<TAG>: <symbol> | <main> | Z: <z> | X: <x> | Y: <y>'.

    Memoized on the description text: the tree label, the tooltip and the
    selection panel all ask for the same split of the same string, and every
    rebuild of the tree asks again."""
    main, axes = _parse_desc_text(getattr(e, "description", "") or "")
    return main, dict(axes)


@_lru_cache(maxsize=65536)
def _parse_desc_text(d):
    segs = [s.strip() for s in d.split("|") if s.strip()]
    if segs and _TAG_LEAD.match(segs[0]):
        segs = segs[1:]
    main = ""
    axes = {}
    for s in segs:
        if len(s) > 2 and s[0] in "XYZ" and s[1] == ":":
            axes[s[0]] = _tr_de(s[2:].strip())
        elif not main:
            main = _tr_de(s)
    return main, axes


import re as _re2

# a label this generic doesn't identify the parameter on its own; the symbol's
# subsystem is prepended to disambiguate it.
_GENERIC_LABELS = {
    "timeout", "switch", "switch type", "signal switch", "counter", "value",
    "factor", "status", "threshold", "offset", "time", "limit", "map",
    "curve", "enable", "default value", "substitute value", "correction",
    "gain", "activation", "deactivation", "selection", "calculation",
    "monitoring", "hysteresis", "duration", "delay", "ratio", "mask", "index",
    "coding table", "reset value", "start value", "min", "max", "gradient",
}

# CAN frame partner tokens (FrmMng/NetMng) -> readable subsystem
_CAN_PARTNER = [
    ("AIRBG", "Airbag"), ("ABS", "ABS"), ("ESP", "ESP"),
    ("ACCS", "ACC sensor"), ("ACC", "ACC"), ("AC", "A/C"),
    ("EGS", "Transmission"), ("TCU", "Transmission"), ("Getr", "Transmission"),
    ("BSG", "Body control"), ("Klima", "Climate control"),
    ("Kombi", "Instrument cluster"), ("LWS", "Steering angle"),
    ("MMI", "MMI"), ("EPB", "Parking brake"), ("AWD", "AWD"),
    ("AAG", "Exhaust flap"), ("PSG", "Pump"),
]


def _can_partner(sym: str) -> str:
    m = _re2.match(r"(?:FrmMng|NetMng)_([A-Za-z]+)", sym)
    if not m:
        return ""
    tok = m.group(1)
    for pre, name in _CAN_PARTNER:
        if tok.startswith(pre):
            return name
    return ""


def _strip_symbols(t: str) -> str:
    """Remove raw Bosch symbol cross-references (AccPed_trqEng#_MAP, Eng_nAvrg,
    FBC_nThresFbc_C) that leak into some descriptions, and tidy the remainder."""
    t = _re2.sub(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9#]+)+\b", "", t)
    t = t.replace("_MAPs", "maps").replace("_MAP", "map").replace("_CUR",
                                                                  "curve")
    t = _re2.sub(r"\s+", " ", t).strip(" ,;:·-")
    # drop an unbalanced trailing bracket left by trimming (e.g. "... stage )")
    if t.endswith((")", "]")) and t.count(t[-1]) > t.count(
            "(" if t[-1] == ")" else "["):
        t = t[:-1].strip(" ,;:·-")
    # drop a dangling trailing function word left by the removal
    t = _re2.sub(r"\b(of the|of|the|for|from|and|or|to|a|an|with|by|via|in|"
                 r"between)\s*$", "", t, flags=_re2.I).strip(" ,;:·-")
    return t


def _delegend(text: str) -> str:
    """Recover a name from a value-legend description. Bosch codes some switch/
    status constants as an enumeration ("0 = counters not reset with 1 = ..."
    or "1: Consideration of ... active"); the leading "N =" / "N:" and any
    second embedded enumerator carry no naming value, so strip them and keep
    the descriptive phrase that names what the code controls."""
    t = _re2.sub(r'^\s*[-+]?\d+\s*[=:]\s*', '', text)   # leading enumerator
    m = _re2.search(r'\s+(?:with\s+|;\s*|,\s*)?[-+]?\d+\s*[=:]', t)  # next one
    if m and m.start() >= 8:
        t = t[:m.start()]
    return t.strip(" ,;:.-")


def _concise(text: str, cap: int = 66) -> str:
    """A scannable label from a long A2L description: cut at the first clause
    boundary that leaves a substantial head, strip any raw symbol reference,
    then cap the length. The full text is kept for the tooltip/detail panel."""
    t = " ".join(text.split())
    for sep in (";", " — ", ", ", " (", " for ", " from ", " if ",
                " when ", " based on ", " out of ", " depending",
                " to select", " between ", " via ", " according",
                " in case", " as a "):
        i = t.find(sep)
        if 12 <= i <= cap:
            t = t[:i]
            break
    t = _strip_symbols(t)
    if len(t) > cap:
        t = t[:cap].rsplit(" ", 1)[0] + "…"
    return t.strip(" -–—:.")


def _is_run(e) -> bool:
    """A one-dimensional table: one row or one column, more than one cell.

    Its definition may store it Nx1 or 1xN -- the reference definitions use
    both for the same parameter -- but it is one run of values either way,
    and everything the user sees (title, shape, comparison, copy) treats it
    as that run, shown along X unless View > Swap X and Y axes says
    otherwise."""
    return (e is not None and getattr(e, "kind", "") != "constant"
            and 1 in (e.rows, e.cols) and e.rows * e.cols > 1)


def _run_cells(e):
    """(row, col) of every cell, in storage order."""
    return [(r, c) for r in range(e.rows) for c in range(e.cols)]


def _cell_pairs(e, other):
    """[((r, c) in e, (r, c) in other)] for two tables holding the same
    shape of data -- identical dimensions, or two runs of equal length
    stored the other way round -- else None."""
    if other is None:
        return None
    if (e.rows, e.cols) == (other.rows, other.cols):
        return [(rc, rc) for rc in _run_cells(e)]
    if _is_run(e) and _is_run(other) \
            and e.rows * e.cols == other.rows * other.cols:
        return list(zip(_run_cells(e), _run_cells(other)))
    return None


def _kind_text(e) -> str:
    if getattr(e, "kind", "") == "constant":
        return "Value"
    if _is_run(e):
        return "Curve"
    return {"map": "Map", "curve": "Curve"}.get(e.kind, e.kind)


def _shape_text(e, transposed=None) -> str:
    """Rows x columns as the table is SHOWN. ``transposed`` is the grid's
    flag -- turned relative to STORAGE, which a stored Nx1 run is by default
    so that it reads along X. None means the default view: a run along X
    (1 x N), a 2-D map as stored."""
    if getattr(e, "kind", "") == "constant":
        return ""
    if transposed is None:
        if _is_run(e):
            return "1 x %d" % (e.rows * e.cols)
        transposed = False
    r, c = e.rows, e.cols
    return "%d x %d" % ((c, r) if transposed else (r, c))


def _ascii_of(e) -> str:
    """The characters an ASCII parameter holds, all of them, in storage
    order (reading only row 0 gave the first character of a 12x1 field)."""
    if getattr(e, "text_kind", "") != "ascii":
        return ""
    try:
        out = "".join(chr(int(round(e.cell(r, c))))
                      if 0x20 <= int(round(e.cell(r, c))) <= 0x7E else "."
                      for r, c in _run_cells(e))
    except Exception:
        return ""
    return out.strip()


def _decoded_label(e) -> str:
    """A ZF-decoded name for this entity, or "" for everything else."""
    try:
        from .zf6hp import view6hp as _v
        return _v.decoded_label(e)
    except Exception:
        return ""


def _label_group_counts(node) -> int:
    """Append "(n)" to every grouping row, as the 6HP tuner did.

    A heading that does not say how much is under it makes the tree something
    to open one node at a time to find out. Counts leaves, not children, so a
    family says how many maps it holds rather than how many sub-groups.
    """
    leaves = 0
    for i in range(node.childCount()):
        child = node.child(i)
        if child.childCount():
            n = _label_group_counts(child)
            # No "already labelled?" guard: _populate clears the tree first,
            # so every heading is fresh. A guard that skipped anything ending
            # in ")" silently dropped the count from "TCC maps (torque
            # converter clutch)" and every other heading with a parenthesis.
            child.setText(0, "%s (%d)" % (child.text(0), n))
            leaves += n
        else:
            leaves += 1
    return leaves


def _english(e) -> str:
    """The tree label: the catalogue name where there is one, otherwise the
    concise description label -- and for a coding-variant copy, which coding
    selects it."""
    curated = _catalogue.display_name(e)
    if curated:
        return curated              # carries the coding itself
    lbl = _english_plain(e)
    coding = getattr(e, "coding", "")
    return "%s — %s" % (lbl, coding) if coding else lbl


def _filter_text(e, label: str) -> str:
    """What "Filter parameters..." matches a row against: its symbol,
    description and definition label, and the label the tree actually shows
    -- which is where a coding ("coding 150031 (5-speed manual)") and a
    selector note live. Matching the catalogue name alone missed the coding
    on every table the catalogue does not name, the gear ratios included."""
    # A copy two codings share reads "coding 150031/158031"; each one on its
    # own must find it too, so "coding 158031" works as typed.
    each = " ".join("coding %s" % c for c in
                    _re0.findall(r"\d+", getattr(e, "coding", "") or ""))
    return "\0".join((e.name.lower(), (e.description or "").lower(),
                      (e.label or "").lower(), label.lower(), each))


def _english_plain(e) -> str:
    """Concise human-readable label: a trimmed main description, disambiguated
    by subsystem when it is too generic; falls back to a decoded symbol name."""
    curated = _catalogue.display_name(e)
    if curated:
        # the tuning catalogue's name, baked into the knowledge base:
        # "Max fuel vs. boost (Smoke Limiter, MAP)". Ahead of the stored
        # label: its selector note follows edits, a stored label would not.
        return curated
    stored = getattr(e, "_ui_label", None)
    if stored is not None:      # read from the file's stored definition
        return stored
    lbl = getattr(e, "label", "")
    if lbl and not e.name:
        # An entity found structurally has no symbol, so its label is the only
        # handle it has -- "Shift schedule SKL 3 - S (sport)". Falling through
        # to the description here showed every schedule on a 6HP19 as the same
        # sentence: "shift schedule found structurally".
        return lbl
    if lbl and lbl != e.name and lbl.startswith(e.name + " - "):
        # EDC15: the knowledge base carries a translated title already
        return lbl[len(e.name) + 3:]
    decoded = _decoded_label(e)
    if decoded:
        # A name the ZF decode or curated table produced -- "Tip-mode upper
        # output speed limit (SKL 27)" -- which the definition's description
        # cannot express: all ten of those describe themselves as "Upper
        # output speed limit". Deliberately NOT every ZF label: most are the
        # description echoed back, and those still need the German
        # translation and the tidying below.
        return decoded
    main, _ = _parse_desc(e)
    if len(main) > 2:
        lbl = _concise(_delegend(main))
        # a pure coding legend can reduce to nothing once its enumerator and
        # symbol reference are stripped; treat that like a missing description
        # and fall through to the decoded symbol name below.
        if len(lbl) >= 3:
            if lbl.lower() in _GENERIC_LABELS or len(lbl) < 5:
                prefix = _can_partner(e.name)
                if not prefix:
                    try:
                        from .signal_names import _MODULE
                        prefix = _MODULE.get(e.name.split("_")[0], "")
                    except Exception:
                        prefix = ""
                if prefix and not lbl.lower().startswith(prefix.lower()):
                    lbl = "%s %s" % (prefix, lbl[:1].lower() + lbl[1:])
            return lbl
    try:
        from .signal_names import human_name
        return human_name(e.name) or e.name
    except Exception:
        return e.name


def _glossary_terms(e):
    """ZF shorthand this symbol contains, for the hover text.

    The definitions name things in abbreviated German -- KW_PSTGNEG, KL_AFZG,
    GFW -- which neither the OLS description nor a German dictionary unpacks,
    because it is ZF's own vocabulary. Advisory: a term appearing in a name is
    not proof the parameter is about it, so this annotates and never renames.
    """
    name = getattr(e, "name", "") or ""
    if not name:
        return []
    try:
        from .zf6hp import glossary6hp as _g
    except Exception:
        return []
    whole = _g.symbol_meaning(name)
    if whole:
        return [(name, whole)]
    return _g.expand(name)[:4]


def _entity_tooltip(e) -> str:
    """Rich hover text: description, symbol, shape, axis descriptions (with
    units), value units and address."""
    import html
    main, axes = _parse_desc(e)
    kind = _kind_text(e)
    shape = _shape_text(e)
    shape = "  " + shape if shape else ""
    lines = []
    curated = _catalogue.display_name(e)
    if curated:
        lines.append("<b>%s</b>" % html.escape(curated))
    lines += ["<b>%s</b>" % html.escape(main or e.name) if not curated
              else html.escape(main or e.name),
              "<code>%s</code> &mdash; %s%s %s"
              % (html.escape(e.name), kind, shape,
                 html.escape(e.units or ""))]
    if getattr(e, "selector", "") and getattr(e, "set_index", ""):
        lines.append("<span style='color:#888'>Set member #%s, picked by "
                     "<code>%s</code>%s</span>"
                     % (html.escape(e.set_index), html.escape(e.selector),
                        (": " + html.escape(e.set_note))
                        if getattr(e, "set_note", "") else
                        " (not in this file)"))
    if axes.get("Z"):
        lines.append("<span style='color:#888'>Value: %s</span>"
                     % html.escape(axes["Z"]))
    for tag, axis in (("X", getattr(e, "x", None)),
                      ("Y", getattr(e, "y", None))):
        if axis is None or not getattr(axis, "count", 0):
            continue
        u = (axis.units or "").strip()
        desc = axes.get(tag, "")
        bits = ["%d pts" % axis.count]
        if u:
            bits.append(html.escape(u))
        label = html.escape(desc) + " " if desc else ""
        lines.append("<span style='color:#888'>%s axis: %s(%s)</span>"
                     % (tag, label, ", ".join(bits)))
    for term, meaning in _glossary_terms(e):
        lines.append("<span style='color:#888'>%s &mdash; %s</span>"
                     % (html.escape(term), html.escape(meaning)))
    try:
        loc = "@ 0x%X" % e.addr
    except Exception:
        loc = ""
    src = getattr(e, "source", "")
    lines.append("<span style='color:#888'>%s &middot; %s &middot; %s</span>"
                 % (loc, html.escape(tier_name(e.tier)), html.escape(src)))
    return "<br>".join(lines)


if HAVE_QT:

    #: item-data role holding a tree heading's text without its "(n)" count
    _BASE_ROLE = QtCore.Qt.UserRole + 1

    class _TreeTips(QtCore.QObject):
        """Hover text for the parameter tree, built when the pointer is over
        a row rather than for all of them up front."""

        def __init__(self, tree):
            super().__init__(tree)
            self.tree = tree

        def eventFilter(self, obj, ev):
            if ev.type() == QtCore.QEvent.ToolTip:
                item = self.tree.itemAt(ev.pos())
                e = (item.data(0, QtCore.Qt.UserRole)
                     if item is not None else None)
                if e is None:
                    tip = item.toolTip(0) if item is not None else ""
                    if tip:     # a group heading: what the group is for
                        QtWidgets.QToolTip.showText(ev.globalPos(), tip, obj)
                    else:
                        QtWidgets.QToolTip.hideText()
                else:
                    QtWidgets.QToolTip.showText(
                        ev.globalPos(), _entity_tooltip(e), obj)
                return True
            return False

    class _TreeMiddleClick(QtCore.QObject):
        """Middle-click on a parameter in the tree opens it in a new table
        tab, as a middle-click on a link does in a browser. The press is
        swallowed so the tree's selection does not move with it."""

        def __init__(self, tree, opener):
            super().__init__(tree)
            self.tree = tree
            self.opener = opener
            self._pressed = None

        def _entity_at(self, pos):
            item = self.tree.itemAt(pos)
            e = item.data(0, QtCore.Qt.UserRole) if item is not None else None
            return e if e is not None and hasattr(e, "rows") else None

        def eventFilter(self, obj, ev):
            t = ev.type()
            if t in (QtCore.QEvent.MouseButtonPress,
                     QtCore.QEvent.MouseButtonRelease,
                     QtCore.QEvent.MouseButtonDblClick) \
                    and ev.button() == QtCore.Qt.MouseButton.MiddleButton:
                e = self._entity_at(ev.position().toPoint())
                if t == QtCore.QEvent.MouseButtonRelease:
                    if e is not None and e is self._pressed:
                        self.opener(e)
                    self._pressed = None
                else:
                    self._pressed = e
                return e is not None or t == QtCore.QEvent.MouseButtonRelease
            return False

    DIFF_BG = QtGui.QColor(255, 214, 153)
    HL_BG = QtGui.QColor(255, 235, 59)            # cells a Find result points at
    EDIT_BG = QtGui.QColor(198, 232, 255)
    AXIS_BG = QtGui.QColor(70, 100, 140)          # X/Y axis breakpoint bands
    AXIS_FG = QtGui.QColor(255, 255, 255)         # white text on the axis bands
    CELL_DARK_FG = QtGui.QColor(20, 20, 20)       # dark text on any light fill
    HEX_EDIT_BG = QtGui.QColor(255, 213, 145)     # byte changed from original
    HEX_DEF_BG = QtGui.QColor(129, 199, 132)      # byte inside a defined table
    HEX_DIFF_BG = QtGui.QColor(239, 154, 154)     # byte differs vs comparison
    SEP_BG = QtGui.QColor(96, 110, 128)           # comparison banner row
    SEP_FG = QtGui.QColor(255, 255, 255)
    HEX_HL_FG = QtGui.QColor(20, 20, 20)          # text on any highlight

    # Grid roles as plain ints. The table model answers data() for every
    # cell of every repaint; looking each role up on Qt's enum classes there
    # was a measurable part of the time a table took to show.
    _R_DISPLAY = int(QtCore.Qt.DisplayRole)
    _R_EDIT = int(QtCore.Qt.EditRole)
    _R_TIP = int(QtCore.Qt.ToolTipRole)
    _R_FONT = int(QtCore.Qt.FontRole)
    _R_ALIGN = int(QtCore.Qt.TextAlignmentRole)
    _R_BG = int(QtCore.Qt.BackgroundRole)
    _R_FG = int(QtCore.Qt.ForegroundRole)
    _R_SIZE = int(QtCore.Qt.SizeHintRole)
    _R_TEXT = frozenset((_R_DISPLAY, _R_EDIT))
    #: everything TableModel.data answers; Qt asks for more, and those
    #: return None before any work is done
    _GRID_ROLES = frozenset((_R_DISPLAY, _R_EDIT, _R_TIP, _R_FONT, _R_ALIGN,
                             _R_BG, _R_FG, _R_SIZE))
    _AL_LEFT = int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    _AL_RIGHT = int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    _AL_CENTER = int(QtCore.Qt.AlignCenter)
    _NO_SELECT_EDIT = ~(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEditable)
    _NO_EDIT = ~QtCore.Qt.ItemIsEditable
    _bold = []

    def _bold_font():
        """One bold font for the grid, made on first use (a QFont wants the
        application to exist) and shared: Qt copies what data() returns."""
        if not _bold:
            f = QtGui.QFont()
            f.setBold(True)
            _bold.append(f)
        return _bold[0]

    import os as _os

    def _ensure_ext(path, ext):
        """Append ``ext`` when the chosen name has no extension (QFileDialog on
        some platforms returns the bare name even with a filter selected)."""
        if path and not _os.path.splitext(path)[1]:
            return path + ext
        return path

    def _filter_ext(selected_filter):
        """".bin" for "Binary files (*.bin)"; None for "All files (*)" or none.

        The extension a bare name gets must be the type the user picked in
        the dialog, not the dialog's default: an EDC15 saved as "tune" with
        "EDC15 container (*.edc15)" picked came out as tune.bin, and with an
        EEPROM held, "Binary files" gave tune.edc15bak."""
        import re
        m = re.search(r"\(\*(\.[A-Za-z0-9_]+)", selected_filter or "")
        return m.group(1).lower() if m else None

    _APP_ICON = None
    _APP_ICON_FILES = ("ovtsuite.ico",)

    def app_icon():
        """The application/window icon (the EDC16 case art), bundled in the
        package's data dir so it resolves both from source and when frozen.
        In a standalone-tool process (``--flasher``, ``--sniffer``,
        ``--logger``) it is that tool's variant instead (see
        :func:`use_tool_icon`), so every window that process opens wears it.
        Cached; returns a null QIcon if no file is found."""
        global _APP_ICON
        if _APP_ICON is None:
            _APP_ICON = QtGui.QIcon()
            for name in _APP_ICON_FILES:
                p = _os.path.join(_os.path.dirname(__file__), "data", name)
                if _os.path.exists(p):
                    _APP_ICON = QtGui.QIcon(p)
                    break
        return _APP_ICON

    def use_tool_icon(tool):
        """Make :func:`app_icon` return a standalone tool's icon -- the app
        icon with that tool's mark (``flasher``: lightning bolt, ``sniffer``:
        magnifier, ``logger``: graph) -- for the rest of this process, falling
        back to the normal icon if the tool's file is missing. Called by
        :func:`_run_tool` before any window exists."""
        global _APP_ICON, _APP_ICON_FILES
        _APP_ICON_FILES = ("ovtsuite_%s.ico" % tool, "ovtsuite.ico")
        _APP_ICON = None

    class FlowLayout(QtWidgets.QLayout):
        """Items left to right, wrapping onto another row when the width runs
        out -- Qt's flow-layout example. A row of buttons in a QHBoxLayout
        cannot shrink, so it set the whole window's minimum width: with a ZF
        shift map on screen the edit bar alone needed 1,209 px, and on a
        smaller screen the right of every tab (and the tab bar's + button)
        was laid out past the window's edge."""

        def __init__(self, parent=None, spacing=6):
            super().__init__(parent)
            self._items = []
            self._gap = spacing

        def addItem(self, item):
            self._items.append(item)

        def count(self):
            return len(self._items)

        def itemAt(self, i):
            return self._items[i] if 0 <= i < len(self._items) else None

        def takeAt(self, i):
            return self._items.pop(i) if 0 <= i < len(self._items) else None

        def expandingDirections(self):
            return QtCore.Qt.Orientation(0)

        def hasHeightForWidth(self):
            return True

        def heightForWidth(self, width):
            return self._place(QtCore.QRect(0, 0, width, 0), dry=True)

        def setGeometry(self, rect):
            super().setGeometry(rect)
            self._place(rect, dry=False)

        def sizeHint(self):
            return self.minimumSize()

        def minimumSize(self):
            size = QtCore.QSize()
            for it in self._shown():
                size = size.expandedTo(it.minimumSize())
            m = self.contentsMargins()
            return size + QtCore.QSize(m.left() + m.right(),
                                       m.top() + m.bottom())

        def _shown(self):
            # a group whose widgets are all hidden (the ZF-only controls on
            # an EDC16 map) takes no room at all
            return [it for it in self._items
                    if not it.isEmpty() and it.sizeHint().width() > 0]

        def _place(self, rect, dry):
            m = self.contentsMargins()
            r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
            x, y, line_h = r.x(), r.y(), 0
            shown = self._shown()
            if not dry:
                # A group left out (every widget in it hidden) still has its
                # container widget. Left unplaced it kept Qt's default
                # 640x480 at the bar's top-left -- over "Modify selection:",
                # and created after it, so on top: the operation dropdown
                # took no clicks on any EDC16 map, where the ZF "Show Z as"
                # group is empty. Collapse it instead.
                for it in self._items:
                    if it not in shown:
                        it.setGeometry(QtCore.QRect(r.x(), r.y(), 0, 0))
            for it in shown:
                hint = it.sizeHint()
                if x > r.x() and x + hint.width() > r.right() + 1:
                    x = r.x()
                    y += line_h + self._gap
                    line_h = 0
                if not dry:
                    it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
                x += hint.width() + self._gap
                line_h = max(line_h, hint.height())
            return y + line_h - rect.y() + m.bottom()

    class _GroupedBar:
        """Builds the edit bar as groups that wrap as a whole: a separator
        or a stretch starts the next group, so "Modify selection: [op]
        [value] Apply" never splits across two rows."""

        def __init__(self, widget):
            self.flow = FlowLayout(widget)
            self.flow.setContentsMargins(0, 0, 0, 0)
            self._next()

        def _next(self):
            box = QtWidgets.QWidget()
            self._row = QtWidgets.QHBoxLayout(box)
            self._row.setContentsMargins(0, 0, 0, 0)
            self.flow.addWidget(box)

        def setContentsMargins(self, *m):
            self.flow.setContentsMargins(*m)

        def addWidget(self, w):
            if isinstance(w, QtWidgets.QFrame) and \
                    w.frameShape() == QtWidgets.QFrame.VLine:
                w.deleteLater()             # the gap between groups is it
                self._next()
                return
            self._row.addWidget(w)

        def addStretch(self, _n=0):
            self._next()

    class OptionsDelegate(QtWidgets.QStyledItemDelegate):
        """A drop-down editor for a value cell whose parameter names its
        values (``entity.options``: raw -> label, e.g. a VDO cluster's coding
        digits). Every other cell gets the ordinary line editor."""

        def _opts(self, index):
            m = index.model()
            e = getattr(m, "entity", None)
            if e is None or getattr(e, "text_kind", ""):
                return None
            opts = getattr(e, "options", None)
            if not opts or m._kind(index.row(), index.column())[0] != "z":
                return None
            return opts

        def createEditor(self, parent, option, index):
            opts = self._opts(index)
            if not opts:
                from . import gui_cellkeys as _gck  # arrows / Ctrl+Enter
                return _gck.editor_created(
                    super().createEditor(parent, option, index), self.parent())
            box = QtWidgets.QComboBox(parent)
            for raw, label in sorted(opts.items()):
                box.addItem("%s  (%d)" % (label, raw), raw)
            return box

        def setEditorData(self, editor, index):
            if not isinstance(editor, QtWidgets.QComboBox) or not self._opts(index):
                return super().setEditorData(editor, index)
            try:
                cur = int(float(index.model().data(index, QtCore.Qt.EditRole)))
            except (TypeError, ValueError):
                cur = None
            i = editor.findData(cur)
            if i < 0 and cur is not None:   # a value no label covers: keep it
                editor.addItem("%d (no name)" % cur, cur)
                i = editor.count() - 1
            editor.setCurrentIndex(max(i, 0))

        def setModelData(self, editor, model, index):
            if not isinstance(editor, QtWidgets.QComboBox) or not self._opts(index):
                return super().setModelData(editor, model, index)
            model.setData(index, str(editor.currentData()), QtCore.Qt.EditRole)

    class TableModel(QtCore.QAbstractTableModel):
        """A single entity's Z data in display units, with the X axis shown as
        an editable top band and the Y axis as an editable left band (like the
        6HP editor). Supports selection-scoped arithmetic and TSV copy/paste."""

        #: An edit was made through this grid (typed, pasted, or a selection
        #: operation) -- the table tab showing it stops being a preview.
        edited = QtCore.Signal()

        def __init__(self, session: TunerSession, readonly: bool = False,
                     other_label: str = "comparison"):
            super().__init__()
            self.session = session
            self.readonly = readonly
            self.other_label = other_label     # what the overlay values are
            self.cmp_name = ""                 # comparison file name (banner)
            self.entity: Optional[Entity] = None
            self.compare: Optional[Entity] = None
            self.overlay = None
            self._vals: List[List[float]] = []
            self._cmp: List[List[float]] = []
            self._x: List[float] = []
            self._y: List[float] = []
            self._dec = 3
            self._cmp_dec = 3
            self._roff = 0                 # 1 when an X-axis band is shown
            self._coff = 0                 # 1 when a Y-axis band is shown
            #: Swap the axes on screen. Display only -- see _kind.
            self.transposed = False
            self._heatmap = False
            #: cells the Find panel is pointing at: {('z'|'x'|'y', a, b)}
            self.hl = frozenset()
            self._vmin = 0.0
            self._vmax = 0.0
            # ZF 6HP only: Z is an output-shaft speed on shift/TCC/tip maps,
            # which is not a number anyone tunes in. Every mode is a pure
            # scaling (see zf6hp.view6hp.z_factor), so cells convert on the
            # way out and back on the way in, and the stored value is never
            # touched by looking at it.
            self.zmode = "raw"
            self.vehicle = None
            self._zheads = None
            self._rheads = None
            self._cx: List[float] = []         # comparison block's X band
            self._cy: List[float] = []         # comparison block's Y band
            #: session revision the values on screen were read at; the
            #: window's timer refreshes when an edit elsewhere moves it
            self._rev = None
            self._ndiff = None              # see _diff_count
            self._ax_diff = frozenset()     # ("x"|"y", i) breakpoints differing
            self._crheads = None            # comparison's own row labels

        def _cmp_axes(self):
            """(X, Y) breakpoints the comparison block's cells sit at.

            Resampled (different size) or identical axes: the primary's --
            the values were put onto them. Same size with different axes:
            the comparison's own, since each value is shown in the cell it is
            stored in, which is a different operating point. Showing the
            primary's breakpoints there would label every cell wrongly."""
            ov, cp, e = self.overlay, self.compare, self.entity
            if ov is not None and ov.mode == "axis-mismatch" and cp is not None:
                cx, cy = cp.x_values(), cp.y_values()
                if (e is not None and 1 in (e.rows, e.cols)
                        and 1 in (cp.rows, cp.cols)
                        and (e.rows == 1) != (cp.rows == 1)):
                    cx, cy = cy, cx     # the overlay turned it (build_overlay)
                return (cx or self._x, cy or self._y)
            return self._x, self._y

        def _recompute_heat(self):
            # one scale over primary AND comparison, so equal values get
            # equal colours in both halves of the grid
            flat = [v for row in self._vals for v in row]
            if self._cmp:
                flat += [v for row in self._cmp for v in row if v is not None]
            self._vmin = min(flat) if flat else 0.0
            self._vmax = max(flat) if flat else 0.0

        def _cell_style(self, v, other, role):
            """Background / foreground / font for a value cell. With the
            heatmap on, heat wins in both halves and a differing cell is
            marked bold; otherwise a differing cell gets the diff fill.

            ``v`` is None for a comparison cell the other calibration has no
            value for -- a TCC map with three sub-columns per gear group laid
            over one with four. Blank, not zero and not a sentinel: there is
            no value there to colour or to call different."""
            if v is None:
                return None
            differs = other is not None and abs(v - other) > 1e-6
            heat = self._heatmap and self._vmax > self._vmin
            if role == _R_BG:
                if heat:
                    return self._heat((v - self._vmin) / (self._vmax - self._vmin))
                return DIFF_BG if differs else None
            if role == _R_FG and (heat or differs):
                # both DIFF_BG and the heat ramp are light fills -> dark text,
                # theme-independent (default text would be white in dark mode)
                return CELL_DARK_FG
            if role == _R_FONT and heat and differs:
                return _bold_font()
            return None

        def _sync_compare(self):
            """Everything the comparison block shows that depends on both
            sides' current values: the overlay, the breakpoints its cells sit
            at, which of those differ from the primary's, and the
            comparison's own row labels. Rebuilt on every change of either
            side -- the overlay's mode (direct / axes differ) follows an axis
            edit or an undo, not just a new table."""
            e, cp = self.entity, self.compare
            # A TCC comparison matches columns by gear group rather than
            # interpolating across them; see view6hp.compare_columns.
            self.overlay = build_overlay(
                e, cp, _view6.compare_columns(e, cp)) if (e and cp) else None
            self._cmp = self.overlay.values if self.overlay else []
            self._ndiff = None
            self._cx, self._cy = self._cmp_axes()
            self._ax_diff = frozenset(
                (ax, i) for ax, i, _a, _b in _axis_changes(e, cp)
            ) if (e and cp and self._cmp) else frozenset()
            # A pedal map labels its rows from its Y axis. The comparison
            # block used the primary's labels, so a saved version or another
            # file whose pedal axis differed showed the current file's axis.
            self._crheads = None
            if cp is not None and self._y_differs():
                # never the primary's labels on rows that sit elsewhere: the
                # comparison's own, else its breakpoints as numbers
                self._crheads = (self._row_labels(cp, self._cy)
                                 or [_fmt(v, 1) for v in self._cy] or None)

        @staticmethod
        def _row_labels(ent, yvals):
            """Row labels for ``ent`` shown at Y breakpoints ``yvals``. A
            label that is its Y axis (a ZF pedal %) is made from ``yvals``,
            so a comparison or saved version shows its own axis and an axis
            edit shows at once; other labels are the entity's own."""
            if ent is None:
                return None
            fmt = getattr(ent, "row_label_fmt", None)
            if fmt and yvals:
                try:
                    return [fmt % v for v in yvals]
                except (TypeError, ValueError):
                    pass
            return (getattr(ent, "row_labels", None)
                    or _view6.row_headers(ent, yvals))

        def _y_differs(self) -> bool:
            return any(ax == "y" for ax, _i in self._ax_diff)

        def _want_coff(self) -> int:
            """1 when the Y band is drawn: always for a plain map, and for a
            pedal map (Y shown as row headers) when the comparison's Y axis
            differs -- the band then shows both sides' breakpoints, which
            the shared column headers of a transposed grid cannot."""
            e = self.entity
            return 1 if (e and self._y and (not self._rheads
                                            or self._y_differs())) else 0

        def set_entity(self, e: Optional[Entity], compare: Optional[Entity]):
            self.beginResetModel()
            self.entity = e
            self.hl = frozenset()           # a search's cells are for one table
            self.compare = compare
            self._vals = e.values() if e else []
            self._x = e.x_values() if e else []
            self._y = e.y_values() if e else []
            self._sync_compare()
            self._dec = _entity_decimals(e) if e else 3
            self._cmp_dec = _entity_decimals(compare) if compare else self._dec
            self._zheads = ((getattr(e, "col_labels", None) or
                             _view6.column_headers(e)) if e else None)
            # per-column explanations (ZF lock-up thresholds: ovtsuite.lockup)
            self._ztips = getattr(e, "col_tips", None) if e else None
            self._rheads = self._row_labels(e, self._y)
            self._roff = 1 if (e and self._x) else 0
            # A pedal map already carries its Y axis in the row headers, in
            # percent. Drawing the Y band as well put the same axis on screen
            # twice -- the header rounded, the band not -- so the band is left
            # off for those and the headers are the one presentation.
            self._coff = self._want_coff()
            self.transposed = self._default_transpose(e)
            if not _view6.z_modes(e):
                self.zmode = "raw"
            self._recompute_heat()
            self._rev = getattr(self.session, "revision", None)
            self.endResetModel()

        @staticmethod
        def _default_transpose(e) -> bool:
            """Show a one-dimensional table along X, whichever way it is stored.

            A 1-D table is the same bytes either way -- a run of values with at
            most one axis -- and which of rows/cols the definition happens to
            set is an accident of how it was written. EDC16 shows them along X
            and so should these; on the RS6 321 of them are stored Nx1 and 1168
            as 1xN, so the stored orientation is not even consistent within one
            file.

            Display only: nothing about the entity or the image changes, and a
            cell keeps the (row, column) it is stored at.
            """
            return bool(e is not None and e.cols == 1 and e.rows > 1)

        def set_transposed(self, on: bool):
            """Swap the X and Y axes on screen."""
            self.beginResetModel()
            self.transposed = bool(on)
            self.endResetModel()

        def set_zmode(self, mode: str, vehicle=None):
            """Show Z in ``mode``. Display only -- nothing is written."""
            self.beginResetModel()
            self.zmode = mode if mode in _view6.Z_MODES else "raw"
            if vehicle is not None:
                self.vehicle = vehicle
            self.endResetModel()

        def _zf(self, col: int) -> float:
            """Scale from stored units to the current display mode."""
            if self.zmode == "raw" or not self.entity:
                return 1.0
            return _view6.z_factor(self.entity, col, self.zmode, self.vehicle)

        def _off(self, v) -> bool:
            """``v`` is the map's stored "off" marker (8HP 10000, 6HP TCC
            full-open) and the Z view converts: it is shown as "off" and never
            scaled, typed or pasted "off" writes it back (see _off_cell)."""
            return (self.entity is not None and _view6.is_converted(self.zmode)
                    and _view6.is_off(self.entity, v))

        def _off_cell(self, a, b, text):
            """``[(a, b, marker)]`` when ``text`` is "off" on a map that has
            an off marker, else None."""
            if (self.entity is None
                    or str(text).strip().lower() != _view6.OFF_TEXT):
                return None
            off = _view6.z_off(self.entity)
            return None if off is None else [(a, b, off)]

        def _ztext(self, v, f, dec) -> str:
            """A stored Z value as the current view shows it."""
            if self._off(v):
                return _view6.OFF_TEXT
            return _fmt(v * f, dec if f == 1.0 else 1)

        def _diff_count(self) -> int:
            """Cells that differ from the comparison; counted once per change
            of values (the banner asks on every repaint)."""
            if self._ndiff is None:
                self._ndiff = sum(
                    1 for r in range(self.entity.rows)
                    for c in range(self.entity.cols)
                    if self._cmp[r][c] is not None
                    and abs(self._vals[r][c] - self._cmp[r][c]) > 1e-6)
            return self._ndiff

        def sep_text(self, full: bool = False) -> str:
            """Banner for the comparison section.

            Short by default: it is spanned across the grid, so it can only be
            as wide as the table, and a narrow map clipped the long form
            mid-word. ``full`` (the tooltip) adds the parameter and the whole
            overlay note, which the description line above shows as well."""
            if not (self.entity and self._cmp):
                return ""
            # A None comparison cell is a column the other calibration does
            # not have, so it is not a difference -- it is an absence.
            n = self._diff_count()
            na = len(self._ax_diff)
            parts = (["%d cell(s) differ" % n] if n else []) + \
                (["%d axis point(s) differ" % na] if na else [])
            diff = ", ".join(parts) if parts else "identical"
            mode = self.overlay.mode if self.overlay is not None else "direct"
            if not full:
                tag = {"axis-mismatch": " · axes differ",
                       "resampled": " · resampled"}.get(mode, "")
                return "Comparison - %s   %s%s" % (
                    self.cmp_name or "comparison", diff, tag)
            note = ""
            if mode != "direct":
                note = "; " + ("axes differ - same size, cells at different "
                               "operating points" if mode == "axis-mismatch"
                               else self.overlay.note)
            what = self.compare.name if self.compare is not None else ""
            return "Comparison - %s:  %s   %s%s" % (
                self.cmp_name or "comparison", what, diff, note)

        def set_heatmap(self, on: bool):
            self._heatmap = bool(on)
            self._recompute_heat()
            if self.entity:
                tl = self.index(0, 0)
                br = self.index(self.rowCount() - 1, self.columnCount() - 1)
                self.dataChanged.emit(tl, br, [QtCore.Qt.BackgroundRole,
                                                QtCore.Qt.ForegroundRole,
                                                QtCore.Qt.FontRole])

        @staticmethod
        def _heat(t: float):
            # cold (blue) -> hot (red) through cyan/green/yellow. Saturated
            # enough to tell bands apart, but light enough that the forced dark
            # cell text stays readable across the whole range in both themes.
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            return QtGui.QColor.fromHsvF((1.0 - t) * 0.66, 0.55, 0.93)

        # -- region classification (grid coords -> data coords) ---------
        # The comparison, when loaded, is part of the SAME grid: below the
        # primary's rows comes a banner row ('sep') and then the comparison's
        # values ('cz', with its own Y band 'cy') on the primary's axes (the
        # overlay resamples when the layouts differ), so one set of scrollers
        # and one column layout serve both.
        def _n_prim(self):
            if not self.entity:
                return 0
            base = self.entity.cols if self.transposed else self.entity.rows
            return base + self._top

        def _cmp_span(self) -> int:
            """Grid rows the comparison block occupies.

            ``self._cmp`` is stored in the entity's own orientation, exactly
            like ``self._vals``, while the grid may be transposed. The block
            mirrors the primary's data rows, so its depth transposes with
            them. Taking ``len(self._cmp)`` regardless claimed entity.rows
            rows for a transposed 1-D table -- 256 of them for a 256x1 curve
            whose transposed comparison is one row deep -- and every grid row
            past the first then indexed off the end of a one-element
            comparison row.
            """
            if not self._cmp:
                return 0
            if self.transposed:
                return max((len(r) for r in self._cmp), default=0)
            return len(self._cmp)

        @staticmethod
        def _at(grid, a, b):
            """``grid[a][b]``, or None where the shape does not reach it.

            A model that raises from ``data()`` does not merely misdraw one
            cell. Qt calls ``data()`` from inside its own layout loops, and
            an exception unwinding out of there tens of thousands of times
            during ``resizeColumnsToContents()`` ended in an access violation
            rather than a traceback. Out of range is a blank cell.
            """
            try:
                return grid[a][b]
            except (IndexError, TypeError):
                return None

        def sep_row(self):
            """Grid row of the comparison banner, or None."""
            return self._n_prim() if (self.entity and self._cmp) else None

        @property
        def _top(self) -> int:
            """1 when a band runs across the top -- the X axis, or Y when
            transposed."""
            return (self._coff if self.transposed else self._roff)

        @property
        def _left(self) -> int:
            """1 when a band runs down the left -- the Y axis, or X when
            transposed."""
            return (self._roff if self.transposed else self._coff)

        def _kind(self, gr, gc):
            """('corner'|'x'|'y'|'z'|'sep'|'cy'|'cz', a, b). a,b are the axis
            index or (r,c).

            Transposing swaps only what the grid shows: the band across the top
            becomes the Y axis and the one down the left the X axis, and a Z
            cell at grid (gr, gc) is the entity's (c, r) rather than its (r, c).
            The entity is untouched, so a value keeps the row and column it is
            stored at and an edit lands on the same byte either way.
            """
            top, left = self._top, self._left
            if top and gr == 0:
                if left and gc == 0:
                    return ("corner", 0, 0)
                return ("y" if self.transposed else "x", gc - left, 0)
            sep = self.sep_row()
            if sep is not None and gr >= sep:
                if gr == sep:
                    return ("sep", 0, gc)
                rr = gr - sep - 1               # row within the block
                if top:
                    if rr == 0:                 # the comparison's own band
                        if left and gc == 0:
                            return ("ccorner", 0, 0)
                        return ("cy" if self.transposed else "cx",
                                gc - left, 0)
                    rr -= 1
                if left and gc == 0:
                    return ("cx" if self.transposed else "cy", rr, 0)
                if self.transposed:
                    return ("cz", gc - left, rr)
                return ("cz", rr, gc - left)
            if left and gc == 0:
                return ("x" if self.transposed else "y", gr - top, 0)
            if self.transposed:
                return ("z", gc - left, gr - top)
            return ("z", gr - top, gc - left)

        def set_highlight(self, cells):
            """Mark cells as search hits: ('z'|'x'|'y', a, b) in the entity's
            own coordinates, so it holds whichever way the grid is turned."""
            new = frozenset(cells)
            if new == self.hl:
                return
            self.hl = new
            nr, nc = self.rowCount(), self.columnCount()
            if nr and nc:
                self.dataChanged.emit(
                    self.index(0, 0), self.index(nr - 1, nc - 1),
                    [QtCore.Qt.BackgroundRole, QtCore.Qt.ForegroundRole])

        def index_of(self, kind, a, b=0):
            """Grid index of the entity cell / axis element (the inverse of
            ``_kind``), or an invalid index when it is not on the grid."""
            top, left = self._top, self._left
            if kind == "z":
                gr, gc = (b + top, a + left) if self.transposed \
                    else (a + top, b + left)
            elif kind == "x":
                gr, gc = (a + top, 0) if self.transposed else (0, a + left)
            elif kind == "y":
                gr, gc = (0, a + left) if self.transposed else (a + top, 0)
            else:
                return QtCore.QModelIndex()
            if 0 <= gr < self.rowCount() and 0 <= gc < self.columnCount():
                return self.index(gr, gc)
            return QtCore.QModelIndex()

        def z_at(self, idx):
            """(r, c) of the Z cell at grid index ``idx``, or None."""
            if not idx.isValid() or not self.entity:
                return None
            k, a, b = self._kind(idx.row(), idx.column())
            return (a, b) if k == "z" else None

        # -- shape ------------------------------------------------------
        def rowCount(self, _=QtCore.QModelIndex()):
            if not self.entity:
                return 0
            base = self.entity.cols if self.transposed else self.entity.rows
            n = base + self._top
            if self._cmp:
                # banner + the comparison's own axis band + its rows
                n += 1 + self._top + self._cmp_span()
            return n

        def columnCount(self, _=QtCore.QModelIndex()):
            if not self.entity:
                return 0
            base = self.entity.rows if self.transposed else self.entity.cols
            return base + self._left

        def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
            if role == QtCore.Qt.ToolTipRole and self.entity:
                return self._header_tip(sec, orient)
            if role != QtCore.Qt.DisplayRole or not self.entity:
                return None
            across, down = ("Y", "X") if self.transposed else ("X", "Y")
            if orient == QtCore.Qt.Horizontal:
                if self._left and sec == 0:
                    return "%s \\ %s" % (down, across) if self._top else down
                col = sec - self._left
                heads = self._rheads if self.transposed else self._zheads
                if heads and 0 <= col < len(heads):
                    return heads[col]
                return str(col)
            if self._top and sec == 0:
                return across
            sep = self.sep_row()
            if sep is not None and sec >= sep:
                rr = sec - sep - 1 - self._top
                if sec == sep:
                    return ""
                if rr < 0:
                    return across               # the comparison's axis band
                return self._rowhead(rr, self._crheads)
            return self._rowhead(sec - self._top)

        def _header_tip(self, sec, orient):
            """The Z column's explanation, wherever that column is drawn."""
            tips = getattr(self, "_ztips", None)
            if not tips:
                return None
            if (orient == QtCore.Qt.Horizontal) != (not self.transposed):
                return None
            if orient == QtCore.Qt.Horizontal:
                col = sec - self._left
            else:
                if self.sep_row() is not None and sec >= self.sep_row():
                    return None
                col = sec - self._top
            return tips[col] if 0 <= col < len(tips) else None

        def _rowhead(self, row: int, cheads=None) -> str:
            heads = (self._zheads if self.transposed
                     else (cheads or self._rheads))
            if heads and 0 <= row < len(heads):
                return heads[row]
            return str(row)

        # -- data -------------------------------------------------------
        def data(self, idx, role=_R_DISPLAY):
            # Qt asks every cell for roles this model never answers
            # (decoration, check state, ...): those leave before any work.
            if role not in _GRID_ROLES or not self.entity or not idx.isValid():
                return None
            k, a, b = self._kind(idx.row(), idx.column())
            tk = getattr(self.entity, "text_kind", "")
            if self.hl and (k, a, b) in self.hl:
                if role == _R_BG:
                    return HL_BG
                if role == _R_FG:
                    return CELL_DARK_FG
            if role == _R_ALIGN:
                if k == "sep":
                    return _AL_LEFT
                if k in ("z", "cz") and tk:
                    # characters and hex bytes read as a row of glyphs, not
                    # as right-aligned quantities -- both halves alike
                    return _AL_CENTER
                return _AL_RIGHT
            if k in ("corner", "ccorner"):
                return None
            if k == "sep":
                if role == _R_SIZE:
                    # The banner is spanned across the whole grid, but its
                    # text lives in column 0 -- left to Qt, that one sentence
                    # set the width of the axis column. Claim no width.
                    return QtCore.QSize(0, 0)
                if role == _R_DISPLAY and b == 0:
                    return self.sep_text()
                if role == _R_TIP:
                    text = self.sep_text(full=True)
                    cs = _ascii_of(self.compare) if self.compare else ""
                    if cs:
                        text += '\n"%s"' % cs  # the comparison's string whole
                    return text
                if role == _R_BG:
                    return SEP_BG
                if role == _R_FG:
                    return SEP_FG
                if role == _R_FONT:
                    return _bold_font()
                return None
            if k == "cz":
                v = self._at(self._cmp, a, b)
                pv = self._at(self._vals, a, b)
                # The same display factor as the primary half. Without it the
                # two halves sat in different units the moment the Z view left
                # "raw" -- 34,035 rpm above 8,160 raw -- and anything copied
                # from one into the other was wrong by the conversion.
                f = self._zf(b)
                if tk == "ascii":
                    # the same characters-not-numbers view as the primary
                    # half, or a changed part number reads as two rows of
                    # unrelated integers
                    if role in _R_TEXT:
                        return "" if v is None else _as_char(v)
                    if role == _R_TIP:
                        return "primary: %s" % ("-" if pv is None
                                                else _as_char(pv))
                    return self._cell_style(v, pv, role)
                if tk == "hex":
                    if role in _R_TEXT:
                        return "" if v is None else "%02X" % (int(round(v)) & 0xFF)
                    if role == _R_TIP:
                        return ("primary: %s"
                                % ("-" if pv is None else "%02X" % (int(round(pv)) & 0xFF)))
                    return self._cell_style(v, pv, role)
                _copts = getattr(self.entity, "options", None)
                if _copts and v is not None and role == _R_DISPLAY:
                    _cl = _copts.get(int(round(v)))
                    return "%s  (%s)" % (_cl, _fmt(v, 0)) if _cl else _fmt(v, 0)
                if role in _R_TEXT:
                    return "" if v is None else self._ztext(v, f, self._cmp_dec)
                if role == _R_TIP:
                    return "primary: %s" % self._ztext(pv, f, self._dec)
                return self._cell_style(v, pv, role)
            if k in ("x", "y", "cx", "cy"):
                vals = {"x": self._x, "y": self._y,
                        "cx": self._cx, "cy": self._cy}[k]
                if a >= len(vals):
                    return None
                if role in _R_TEXT:
                    return _fmt(vals[a], 3)
                differs = (k[-1], a) in self._ax_diff
                # explicit bg AND fg so the band reads the same in light/dark;
                # a breakpoint the other side does not share is marked like a
                # differing cell
                if role == _R_BG:
                    return DIFF_BG if differs else AXIS_BG
                if role == _R_FG:
                    return CELL_DARK_FG if differs else AXIS_FG
                if role == _R_FONT:
                    return _bold_font()
                return None
            # z
            v = self._at(self._vals, a, b)
            if tk == "ascii":
                # A part number is characters, not numbers. hp6tuner showed
                # these as text; reading 09L927156C as twelve integers is
                # what the port was doing.
                if role in _R_TEXT:
                    return _as_char(v)
                return self._cell_style(v, self._at(self._cmp, a, b)
                                        if self._cmp else None, role)
            if tk == "hex":
                # A patch/bypass site is a raw byte the user matches against a
                # hex signature ("stock 6C, bypass 60"), not a scaled number.
                if role in _R_TEXT:
                    return "" if v is None else "%02X" % (int(round(v)) & 0xFF)
                return self._cell_style(v, self._at(self._cmp, a, b)
                                        if self._cmp else None, role)
            opts = getattr(self.entity, "options", None)
            if opts and v is not None:
                lbl = opts.get(int(round(v)))
                if role == _R_DISPLAY:
                    return "%s  (%s)" % (lbl, _fmt(v, 0)) if lbl else _fmt(v, 0)
                if role == _R_EDIT:
                    return _fmt(v, 0)
                if role == _R_TIP:
                    cv = self._at(self._cmp, a, b) if self._cmp else None
                    head = ("%s: %s\n" % (self.other_label, opts.get(
                        int(round(cv)), _fmt(cv, 0))) if cv is not None else "")
                    return head + "\n".join("%d = %s" % (k2, t)
                                             for k2, t in sorted(opts.items()))
            f = self._zf(b)
            if role in _R_TEXT:
                return self._ztext(v, f, self._dec)
            if role == _R_TIP and self._cmp:
                cv = self._at(self._cmp, a, b)
                return "%s: %s" % (self.other_label,
                                   "-" if cv is None
                                   else self._ztext(cv, f, self._cmp_dec))
            if role == _R_TIP and self._off(v):
                return ("Stored %s = off (not a speed, so not converted). "
                        "Type a speed to switch it on, or \"off\" to restore "
                        "it." % _fmt(v, 0))
            return self._cell_style(v, self._at(self._cmp, a, b) if self._cmp
                                    else None, role)

        def flags(self, idx):
            f = super().flags(idx)
            if not self.entity:
                return f
            k, a, b = self._kind(idx.row(), idx.column())
            if k in ("corner", "ccorner", "sep"):
                return f & _NO_SELECT_EDIT
            if k in ("cz", "cx", "cy") or self.readonly:
                return f & _NO_EDIT  # comparison is view-only
            if getattr(self.entity, "read_only", False):
                # a definition marks it so (a cluster's odometer, immobilizer
                # data): carried by patches and transfers, never hand-edited
                return f & _NO_EDIT
            f |= QtCore.Qt.ItemIsEditable             # provenance is advisory, not a lock
            return f

        def _edit_cells(self, cells):
            self.session.edit_cells(self.entity, cells)
            self.edited.emit()

        def _edit_axis(self, axis, i, value):
            self.session.edit_axis(axis, i, value)
            self.edited.emit()

        def setData(self, idx, value, role=QtCore.Qt.EditRole):
            if role != QtCore.Qt.EditRole or not self.entity or self.readonly:
                return False
            text = str(value)
            k, a, b = self._kind(idx.row(), idx.column())
            if k in ("x", "y"):
                try:
                    val = float(text.strip())       # breakpoints are numbers
                except (TypeError, ValueError):
                    return False
            if k == "x":
                self._edit_axis(self.entity.x, a, val)
                self._x = self.entity.x_values()
            elif k == "y":
                self._edit_axis(self.entity.y, a, val)
                self._y = self.entity.y_values()
            if k in ("x", "y"):
                # the comparison band shows the primary's breakpoints unless
                # its own differ, and whether they differ may just have
                # changed: rebuild the comparison side (refresh)
                self.refresh()
                return True
            if k != "z":
                return False
            cells = self._text_cells(a, b, text)
            if cells is None:
                cells = self._off_cell(a, b, text)
            if cells is None:
                try:
                    val = float(text.strip())
                except (TypeError, ValueError):
                    return False
                # typed in whatever mode is on screen -> stored units
                f = self._zf(b)
                cells = [(a, b, val / f if f else val)]
            if not cells:
                return False
            self._edit_cells(cells)
            if len(cells) == 1:
                self._vals[a][b] = self.entity.cell(a, b)
                self._ndiff = None
                self._rev = getattr(self.session, "revision", None)
                self.dataChanged.emit(idx, idx)
            else:
                self.refresh()              # a typed string filled a run
            return True

        def _text_cells(self, a, b, text):
            """The cells an entry typed or pasted into Z cell (a, b) writes,
            for a parameter shown as characters or bytes; None for an
            ordinary number cell, [] for an entry that is not valid here.

            ``ascii``: each character is stored as its own code -- "2" is
            0x32, never the number 2 (which is an unprintable byte and read
            back as "."). Several characters fill that many cells onward, so
            a part number can be typed in one go; what does not fit is
            dropped. ``0xNN`` writes a raw byte (e.g. 0x00 padding).
            ``hex``: one byte as shown, "60" or "6C" (or "0x6C")."""
            tk = getattr(self.entity, "text_kind", "")
            opts = getattr(self.entity, "options", None)
            if opts and tk == "":
                t = (text or "").strip()
                for k2, lbl in opts.items():
                    if t.lower() == lbl.lower():
                        return [(a, b, float(k2))]
                import re as _re
                m = _re.search(r"\((\d+)\)\s*$", t)   # "Germany  (0)", as copied
                if m:
                    return [(a, b, float(m.group(1)))]
                return None                 # a number: the normal path
            if tk == "hex":
                try:
                    v = int(text.strip(), 16)
                except (TypeError, ValueError):
                    return []
                return [(a, b, float(v))] if 0 <= v <= 0xFF else []
            if tk != "ascii":
                return None
            if text == "":
                return []
            t = text.strip()
            if len(t) > 2 and t[:2].lower() == "0x":
                try:
                    v = int(t, 16)
                except ValueError:
                    return []
                return [(a, b, float(v))] if 0 <= v <= 0xFF else []
            order = [(r, c) for r in range(self.entity.rows)
                     for c in range(self.entity.cols)]
            try:
                start = order.index((a, b))
            except ValueError:
                return []
            out = []
            for ch, (r, c) in zip(text, order[start:]):
                if ord(ch) > 0xFF:
                    return []               # not a single byte
                out.append((r, c, float(ord(ch))))
            return out

        # -- bulk selection operations ---------------------------------
        def apply_op(self, indexes, op: str, value: float) -> int:
            """Apply an arithmetic op to the Z cells among ``indexes`` in one
            undoable step. op in {set, add, sub, mul, div}. Returns #cells.

            ``value`` is in whatever mode is on screen. For mul and div that
            makes no difference -- every display mode is a pure scaling -- but
            "add 200" with engine RPM showing has to mean 200 engine RPM, and
            in first gear that is 48 stored counts, not 200. In a converted
            view an "off" cell is left alone by add/sub/mul/div (only "set"
            switches it on).
            """
            if not self.entity:
                return 0
            cells = []
            for ix in indexes:
                z = self.z_at(ix)
                if z is None:
                    continue
                r, c = z
                old = self._vals[r][c]
                if op != "set" and self._off(old):
                    continue        # "off" is no speed to add to or scale
                f = self._zf(c) or 1.0
                shown = value / f if op in ("set", "add", "sub") else value
                if op == "set":
                    nv = shown
                elif op == "add":
                    nv = old + shown
                elif op == "sub":
                    nv = old - shown
                elif op == "mul":
                    nv = old * value
                elif op == "div":
                    nv = old / value if value else old
                else:
                    nv = old
                cells.append((r, c, nv))
            if not cells:
                return 0
            self._edit_cells(cells)
            for r, c, _ in cells:
                self._vals[r][c] = self.entity.cell(r, c)
            self.refresh()
            return len(cells)

        def copy_tsv(self, indexes) -> str:
            """TSV of the selected rectangle (display strings; axis cells
            included positionally)."""
            if not indexes:
                return ""
            rows = [ix.row() for ix in indexes]
            cols = [ix.column() for ix in indexes]
            r0, r1, c0, c1 = min(rows), max(rows), min(cols), max(cols)
            sel = {(ix.row(), ix.column()) for ix in indexes}
            out = []
            for gr in range(r0, r1 + 1):
                line = []
                for gc in range(c0, c1 + 1):
                    if (gr, gc) in sel:
                        txt = self.data(self.index(gr, gc),
                                        QtCore.Qt.DisplayRole)
                        k, a, b = self._kind(gr, gc)
                        if (k == "z" and getattr(self.entity, "text_kind", "")
                                == "ascii"):
                            v = self._at(self._vals, a, b)
                            if v is not None and _as_char(v) != chr(
                                    int(round(v)) & 0xFF):
                                txt = "0x%02X" % (int(round(v)) & 0xFF)
                        line.append("" if txt is None else str(txt))
                    else:
                        line.append("")
                out.append("\t".join(line))
            return "\n".join(out)

        def paste_tsv(self, anchor, text: str) -> int:
            """Paste TSV with its top-left at ``anchor``; Z cells batch into one
            undoable edit, axis cells write individually. Returns #cells."""
            if not self.entity or not text.strip():
                return 0
            grid = [ln.split("\t") for ln in text.replace("\r", "").split("\n")
                    if ln != ""]
            gr0, gc0 = anchor.row(), anchor.column()
            zcells, naxis = [], 0
            is_ascii = getattr(self.entity, "text_kind", "") == "ascii"
            for dr, line in enumerate(grid):
                for dc, raw in enumerate(line):
                    tok = raw.strip()
                    # a copied space is a character in an ASCII cell
                    if tok == "" and (raw == "" or not is_ascii):
                        continue
                    gr, gc = gr0 + dr, gc0 + dc
                    if gr >= self.rowCount() or gc >= self.columnCount():
                        continue
                    k, a, b = self._kind(gr, gc)
                    if k == "z":
                        cells = self._text_cells(a, b, raw if is_ascii else tok)
                        if cells is None:
                            cells = self._off_cell(a, b, tok)  # a copied "off"
                        if cells is not None:
                            zcells.extend(cells)
                            continue
                    try:
                        val = float(tok)
                    except ValueError:
                        continue
                    if k == "z":
                        # Paste what you see. The cell displays stored units
                        # scaled to the current Z view, so a value typed or
                        # pasted in engine-rpm view has to come back through
                        # the same factor -- otherwise copying a column and
                        # pasting it one across, in the view a tuner actually
                        # works in, wrote a number 4x too big.
                        f = self._zf(b)
                        zcells.append((a, b, val / f if f else val))
                    elif k == "x":
                        self._edit_axis(self.entity.x, a, val)
                        naxis += 1
                    elif k == "y":
                        self._edit_axis(self.entity.y, a, val)
                        naxis += 1
            if zcells:
                self._edit_cells(zcells)
            self._x = self.entity.x_values()
            self._y = self.entity.y_values()
            if self.entity:
                self._vals = self.entity.values()
            self.refresh()
            return len(zcells) + naxis

        def _z_rect(self, indexes):
            """Bounding rectangle (r0, r1, c0, c1) of the Z cells among
            ``indexes``, or None if none are Z cells."""
            zs = [self.z_at(ix) for ix in indexes]
            zs = [z for z in zs if z is not None]
            if not zs:
                return None
            rs = [r for r, _ in zs]
            cs = [c for _, c in zs]
            return min(rs), max(rs), min(cs), max(cs)

        def interpolate(self, indexes, mode: str) -> int:
            """Fill the selection's bounding rectangle by linear interpolation.
            mode: 'h' (per row, left->right), 'v' (per column, top->bottom),
            '2d' (bilinear from the four corners). One undoable step."""
            if not self.entity:
                return 0
            rect = self._z_rect(indexes)
            if rect is None:
                return 0
            r0, r1, c0, c1 = rect
            g = self._vals
            cells = []
            if mode == "h":
                if c1 == c0:
                    return 0
                for r in range(r0, r1 + 1):
                    lo, hi = g[r][c0], g[r][c1]
                    for c in range(c0 + 1, c1):
                        t = (c - c0) / (c1 - c0)
                        cells.append((r, c, lo + (hi - lo) * t))
            elif mode == "v":
                if r1 == r0:
                    return 0
                for c in range(c0, c1 + 1):
                    lo, hi = g[r0][c], g[r1][c]
                    for r in range(r0 + 1, r1):
                        t = (r - r0) / (r1 - r0)
                        cells.append((r, c, lo + (hi - lo) * t))
            else:                                  # 2d bilinear
                if r1 == r0 or c1 == c0:
                    return 0
                v00, v01 = g[r0][c0], g[r0][c1]
                v10, v11 = g[r1][c0], g[r1][c1]
                for r in range(r0, r1 + 1):
                    tr = (r - r0) / (r1 - r0)
                    for c in range(c0, c1 + 1):
                        tc = (c - c0) / (c1 - c0)
                        top = v00 + (v01 - v00) * tc
                        bot = v10 + (v11 - v10) * tc
                        nv = top + (bot - top) * tr
                        if abs(nv - g[r][c]) > 1e-12:
                            cells.append((r, c, nv))
            if not cells:
                return 0
            self._edit_cells(cells)
            for r, c, _ in cells:
                self._vals[r][c] = self.entity.cell(r, c)
            self.refresh()
            return len(cells)

        def smooth(self, indexes) -> int:
            """One 3x3 neighbour-average smoothing pass over the selected Z
            cells (reads the pre-pass values). One undoable step."""
            if not self.entity:
                return 0
            zs = [z for z in (self.z_at(ix) for ix in indexes) if z is not None]
            if len(zs) < 2:
                return 0
            g = self._vals
            R, C = self.entity.rows, self.entity.cols
            cells = []
            for r, c in zs:
                tot = 0.0
                cnt = 0
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        rr, cc = r + dr, c + dc
                        if 0 <= rr < R and 0 <= cc < C:
                            tot += g[rr][cc]
                            cnt += 1
                nv = tot / cnt
                if abs(nv - g[r][c]) > 1e-12:
                    cells.append((r, c, nv))
            if not cells:
                return 0
            self._edit_cells(cells)
            for r, c, _ in cells:
                self._vals[r][c] = self.entity.cell(r, c)
            self.refresh()
            return len(cells)

        def refresh(self):
            if self.entity is not None and self._vals and (
                    len(self._vals) != self.entity.rows
                    or len(self._vals[0]) != self.entity.cols):
                # the table was resized (or a resize undone): a new grid
                self.set_entity(self.entity, self.compare)
                return
            if self.entity:
                self._vals = self.entity.values()
                self._x = self.entity.x_values()
                self._y = self.entity.y_values()
                self._rheads = self._row_labels(self.entity, self._y)
                self._sync_compare()
                self._recompute_heat()
                coff = self._want_coff()
                if coff != self._coff:
                    # the Y band comes or goes: the grid's shape changes
                    self.beginResetModel()
                    self._coff = coff
                    self.endResetModel()
                    self._rev = getattr(self.session, "revision", None)
                    return
                # Values and headers change, never the shape: a repaint of
                # the grid, not the relayout layoutChanged asks the view for.
                nr, nc = self.rowCount(), self.columnCount()
                if nr and nc:
                    self.dataChanged.emit(self.index(0, 0),
                                          self.index(nr - 1, nc - 1))
                    self.headerDataChanged.emit(QtCore.Qt.Vertical, 0, nr - 1)
                    self.headerDataChanged.emit(QtCore.Qt.Horizontal, 0, nc - 1)
            self._rev = getattr(self.session, "revision", None)

    import math as _math

    def _entity_decimals(e, cap=6) -> int:
        """Decimal places to show for an entity, from its conversion
        resolution (display units per raw LSB). One value per table means one
        decimal count for the whole table, so columns line up. 0 for integer
        resolutions, capped so an odd non-decimal factor (e.g. 1/8192) stays
        readable."""
        step = abs(getattr(getattr(e, "eq", None), "factor", 0.0) or 0.0)
        if step <= 0 or step >= 1:
            return 0
        return min(cap, max(1, _math.ceil(-_math.log10(step) - 1e-9)))

    def _fmt(v: float, dec: int = 3) -> str:
        return "%.*f" % (dec, v)

    def _as_char(v: float) -> str:
        """One stored byte as the character it is, or a dot when it is not one."""
        try:
            code = int(round(v))
        except (TypeError, ValueError):
            return "."
        return chr(code) if 0x20 <= code <= 0x7E else "."

    def Zf6hpDtcDialog(session, parent=None, compare=None):
        """The ZF 6HP fault-code window -- the shared gearbox window
        (:mod:`ovtsuite.gui_tcu_dtc`) on the 6HP module's model. Kept under
        this name for callers that open it directly."""
        from .gui_tcu_dtc import open_for
        return open_for(session, parent, compare=compare)

    class SignalDelegate(QtWidgets.QStyledItemDelegate):
        """Editable combobox of signal ids (named where the catalog knows
        them) for the measuring-block channel cells. Still accepts a typed
        id not in the list, so nothing is locked out.

        The cell's id lives in its UserRole, not its text: the text is a
        label ("12 - Engine Speed"), and reading the id back out of it is how
        an untouched editor used to fall back to id 0 -- engine speed -- and
        write it into the slot on Enter."""

        def __init__(self, choices, catalog, parent=None):
            super().__init__(parent)
            self.choices = choices            # list of (id, label)
            self.catalog = catalog
            self.popup_next = False           # a click opened it: show the list

        def paint(self, painter, option, index):
            view = option.widget
            if view is not None and hasattr(view, "indexWidget") \
                    and view.indexWidget(index) is not None:
                # the cell's editor is open over it: its text under the
                # editor's (translucent in the Windows style) read twice
                opt = QtWidgets.QStyleOptionViewItem(option)
                self.initStyleOption(opt, index)
                opt.text = ""
                view.style().drawControl(QtWidgets.QStyle.CE_ItemViewItem,
                                         opt, painter, view)
                return
            super().paint(painter, option, index)
            if not (index.flags() & QtCore.Qt.ItemIsEditable):
                return
            # a drop-down arrow says the cell is a list to pick from
            opt = QtWidgets.QStyleOption()
            opt.initFrom(option.widget or self.parent())
            side = max(6, min(10, option.rect.height() - 6))
            r = option.rect
            opt.rect = QtCore.QRect(r.right() - side - 4,
                                    r.top() + (r.height() - side) // 2,
                                    side, side)
            if option.state & QtWidgets.QStyle.State_Selected:
                opt.palette.setColor(QtGui.QPalette.ButtonText,
                                     option.palette.highlightedText().color())
            st = (option.widget.style() if option.widget is not None
                  else QtWidgets.QApplication.style())
            st.drawPrimitive(QtWidgets.QStyle.PE_IndicatorArrowDown, opt, painter)

        def createEditor(self, parent, option, index):
            cb = QtWidgets.QComboBox(parent)
            cb.setEditable(True)
            cb.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
            # opaque: nothing of the cell under it shows through
            cb.setAutoFillBackground(True)
            cb.lineEdit().setAutoFillBackground(True)
            for sid, label in self.choices:
                cb.addItem(label, sid)
            cb.setMaxVisibleItems(24)
            # widen the dropdown so long signal names aren't truncated
            fm = cb.fontMetrics()
            longest = max((fm.horizontalAdvance(lbl)
                           for _, lbl in self.choices), default=240)
            cb.view().setMinimumWidth(min(longest + 64, 920))
            cb.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToContentsOnFirstShow)
            # let typing filter the long list instead of only jumping to a prefix
            comp = cb.completer()
            if comp is not None:
                comp.setFilterMode(QtCore.Qt.MatchContains)
                comp.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)

            # picking from the list is the edit: no Enter or click-away needed
            def _picked(_i, cb=cb):
                self.commitData.emit(cb)
                self.closeEditor.emit(cb, QtWidgets.QAbstractItemDelegate.NoHint)
            cb.activated.connect(_picked)
            if self.popup_next:
                self.popup_next = False
                QtCore.QTimer.singleShot(0, cb.showPopup)
            return cb

        def setEditorData(self, editor, index):
            cur = index.data(QtCore.Qt.UserRole)
            if cur is None:
                txt = str(index.data(QtCore.Qt.EditRole) or "").strip()
                head = txt.split(" ", 1)[0]
                cur = int(head) if head.isdigit() else None
            if cur is None:
                editor.setEditText("")
                return
            i = editor.findData(int(cur))
            if i >= 0:
                editor.setCurrentIndex(i)
            else:
                editor.setEditText(str(cur))

        def setModelData(self, editor, model, index):
            txt = editor.currentText().strip()
            sid = editor.currentData()
            i = editor.findText(txt)
            if i < 0 or editor.itemData(i) != sid:
                # typed value: accept a plain integer, or "N - name"
                head = txt.split(" ", 1)[0]
                try:
                    sid = int(head, 0)
                except ValueError:
                    try:
                        sid = int(head)        # "007" is not valid base-0
                    except ValueError:
                        return
            if sid is None:
                return
            if index.data(QtCore.Qt.UserRole) == int(sid):
                return                         # unchanged: no write, no undo step
            model.setData(index, str(int(sid)), QtCore.Qt.EditRole)

    def MsBlockDialog(session, parent=None, compare=None):
        """The measuring-block window (:mod:`ovtsuite.gui_msblock`)."""
        from .gui_msblock import MsBlockDialog as _Dlg
        return _Dlg(session, parent, compare=compare)

    class GearRatioDialog(QtWidgets.QDialog):
        """Drivetrain ratios grouped into two editable tables: absolute ratios
        (gears, reverse, transfer case, final drive) and the v/n detection
        bands (min/max per gear). A dropdown selects which coding-selected copy
        (variant table) to edit; a 'copy edits to all copies' checkbox mirrors
        every edit into all detected copies. Edits are undoable."""

        def __init__(self, session: TunerSession, parent=None, compare=None):
            super().__init__(parent)
            self.session = session
            self.gears = GearRatios(session)
            self.copies = self.gears.copies()
            self.pbn = self.gears._by_name
            self._compare = compare
            self._mainwin = parent
            self.cmp_gears = None
            self.cmp_pbn = {}
            self.cmp_copies = []
            if compare is not None:
                try:
                    self.cmp_gears = GearRatios(compare)
                    self.cmp_pbn = self.cmp_gears._by_name
                    self.cmp_copies = self.cmp_gears.copies()
                    if not self.cmp_copies:
                        self.cmp_gears = None
                except Exception:
                    self.cmp_gears = None
            self._has_cmp = self.cmp_gears is not None
            try:
                self._orig_circ = self.gears.effective_circ(self.copies[0])
            except Exception:
                self._orig_circ = None
            self.cur = 0
            self.setWindowTitle("Gear ratios")
            self.setWindowIcon(app_icon())
            self.resize(720 if self._has_cmp else 520, 600)
            v = QtWidgets.QVBoxLayout(self)

            ncopy = len(self.copies)
            # copy selector + "apply to all" control
            sel = QtWidgets.QHBoxLayout()
            sel.addWidget(QtWidgets.QLabel("Variant:"))
            self.copy_box = QtWidgets.QComboBox()
            for i, c in enumerate(self.copies):
                self.copy_box.addItem(c.label, i)
            self.copy_box.setEnabled(ncopy > 1)
            self.copy_box.currentIndexChanged.connect(self._copy_changed)
            sel.addWidget(self.copy_box)
            sel.addStretch(1)
            self.copy_all = QtWidgets.QCheckBox("Copy edits to all copies")
            self.copy_all.setEnabled(ncopy > 1)
            self.copy_all.setToolTip(
                "When ticked, editing a value writes the same value into the "
                "matching parameter of every detected copy.")
            sel.addWidget(self.copy_all)
            v.addLayout(sel)
            if ncopy == 1:
                only = QtWidgets.QLabel("This binary has a single stored copy.")
                only.setStyleSheet("color: gray")
                v.addWidget(only)

            # EDC15 stores no drivetrain ratios: its first table holds the
            # gearbox switches and v/n plausibility limits instead
            self._has_ratios = hasattr(self.gears, "plan_vn")
            if self._has_ratios:
                v.addWidget(QtWidgets.QLabel(
                    "<b>Absolute ratios</b> &mdash; Gearbx_rTransN are overall "
                    "engine-to-wheel ratios (gearbox &times; final drive); the "
                    "ACCCD value is the differential ratio used by the ACC "
                    "module only"))
                _vlbl = "Ratio"
            else:
                _gh = QtWidgets.QLabel(
                    "<b>Gearbox settings</b> &mdash; gearbox type, gear source "
                    "and v/n plausibility limits. This ECU stores no drivetrain "
                    "ratios: it recognises the gear from the v/n thresholds "
                    "below.")
                _gh.setWordWrap(True)
                v.addWidget(_gh)
                _vlbl = "Value"
            _acols = ["Element", _vlbl] + (
                ["%s (cmp)" % _vlbl] if self._has_cmp else [])
            self.abs_tbl = QtWidgets.QTableWidget(0, len(_acols))
            self.abs_tbl.setHorizontalHeaderLabels(_acols)
            self.abs_tbl.verticalHeader().setVisible(False)
            v.addWidget(self.abs_tbl)

            v.addWidget(QtWidgets.QLabel(
                "<b>V/N ratios</b> &mdash; speed/rpm gear-detection band "
                "(min &amp; max) per gear"))

            # display-mode controls for the v/n table
            self._last_rpm = 1000.0
            self._last_speed = 100.0
            ctl = QtWidgets.QHBoxLayout()
            ctl.addWidget(QtWidgets.QLabel("Show as:"))
            self.vn_mode = QtWidgets.QComboBox()
            self.vn_mode.addItem("Speed at a reference RPM", "speed")
            self.vn_mode.addItem("RPM at a reference speed", "rpm")
            self.vn_mode.addItem("Raw v/n ratio  [(km/h)/rpm]", "ratio")
            ctl.addWidget(self.vn_mode)
            self.vn_ref = QtWidgets.QDoubleSpinBox()
            ctl.addWidget(self.vn_ref)
            self.vn_unit_lbl = QtWidgets.QLabel()
            self.vn_unit_lbl.setStyleSheet("color: gray")
            ctl.addWidget(self.vn_unit_lbl)
            ctl.addStretch(1)
            v.addLayout(ctl)

            _vcols = ["Gear", "Min", "Max"] + (
                ["Min (cmp)", "Max (cmp)"] if self._has_cmp else [])
            self.vn_tbl = QtWidgets.QTableWidget(0, len(_vcols))
            self.vn_tbl.setHorizontalHeaderLabels(_vcols)
            self.vn_tbl.verticalHeader().setVisible(False)
            v.addWidget(self.vn_tbl)

            self._apply_mode_ui()          # configure the spinbox for the mode
            self.vn_mode.currentIndexChanged.connect(self._mode_changed)
            self.vn_ref.valueChanged.connect(self._ref_changed)

            note = QtWidgets.QLabel(
                "Editing writes physical ratio values and is undoable. A value "
                "shared with the main block (no separate variant storage) is "
                "shown in grey.")
            note.setStyleSheet("color: gray")
            note.setWordWrap(True)
            v.addWidget(note)

            btns = QtWidgets.QHBoxLayout()
            gen = QtWidgets.QPushButton("Auto-generate V/N from ratios…")
            gen.setToolTip("Recompute the v/n gear-detection bands from the "
                           "absolute ratios and the tyre size (after a gearbox / "
                           "final-drive or tyre change). v/n = 0.06*C/R; the VSS "
                           "pulse count is NOT used. CAN cars also fold in the "
                           "transfer-case ratio.")
            gen.clicked.connect(self._auto_vn)
            if not self._has_ratios:
                gen.setEnabled(False)
                gen.setToolTip(
                    "Not available for this ECU: it stores no gear or "
                    "final-drive ratios to generate the v/n bands from. Edit "
                    "the v/n thresholds directly (\"Show as: Speed at a "
                    "reference RPM\" makes that easy).")
            btns.addWidget(gen)
            btns.addStretch(1)
            if self._has_cmp:
                cpb = QtWidgets.QPushButton("Copy from comparison")
                cpb.setToolTip("Copy every ratio and v/n band value from the "
                               "comparison binary into the selected variant. "
                               "One undoable step.")
                cpb.clicked.connect(self._copy_gears_from_cmp)
                btns.addWidget(cpb)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.accept)
            btns.addWidget(close)
            v.addLayout(btns)

            self._loading = False
            self.abs_tbl.itemChanged.connect(self._changed)
            self.vn_tbl.itemChanged.connect(self._changed)
            self._populate()

        def _ask_tire(self, hint=None):
            """Modal tyre-size entry; returns the geometric rolling circumference
            in metres, or None if cancelled."""
            import math as _m
            d = QtWidgets.QDialog(self)
            d.setWindowTitle("Tyre size")
            fl = QtWidgets.QFormLayout(d)
            wsp = QtWidgets.QSpinBox(); wsp.setRange(125, 395); wsp.setSingleStep(5)
            wsp.setValue(195); wsp.setSuffix(" mm")
            asp = QtWidgets.QSpinBox(); asp.setRange(25, 85); asp.setSingleStep(5)
            asp.setValue(65); asp.setSuffix(" %")
            rsp = QtWidgets.QSpinBox(); rsp.setRange(12, 24); rsp.setValue(15)
            rsp.setSuffix(" in")
            out = QtWidgets.QLabel()
            out.setStyleSheet("font-weight:600")

            def circ():
                dia = rsp.value() * 25.4 + 2.0 * wsp.value() * asp.value() / 100.0
                return _m.pi * dia / 1000.0

            def upd():
                out.setText("Rolling circumference: %.3f m" % circ())
            for sb in (wsp, asp, rsp):
                sb.valueChanged.connect(upd)
            fl.addRow("Width", wsp)
            fl.addRow("Aspect ratio", asp)
            fl.addRow("Rim diameter", rsp)
            if hint:
                hl = QtWidgets.QLabel("Inferred from the current bands: %.3f m" % hint)
                hl.setStyleSheet("color:gray")
                fl.addRow(hl)
            fl.addRow(out)
            bb = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            fl.addRow(bb)
            upd()
            if not d.exec():
                return None
            return circ()

        def _auto_vn(self):
            if not self._has_ratios:
                return
            gr = self.gears
            copy = self.copies[self.cur]
            can = gr.signal_is_can()
            # Tyre size is always needed (it varies); the circumference inferred
            # from the existing bands+ratios pre-fills it. (VSS pulse count / 
            # ImpPerDist is deliberately NOT used in the v/n math.)
            hint = None
            try:
                hint = gr.effective_circ(copy) or self._orig_circ
            except Exception:
                hint = self._orig_circ
            circ = self._ask_tire(hint)
            if circ is None:
                return
            plan = gr.plan_vn(copy, circ_m=circ, fold_transfer=can)
            if plan.get("need_tire"):               # (shouldn't happen: circ given)
                return
            writes = plan.get("writes") or []
            if not writes:
                QtWidgets.QMessageBox.information(
                    self, "Auto-generate V/N",
                    "No absolute gear ratios were found to generate from.")
                return
            src = ("CAN speed source" if can
                   else "HW FGG sensor")
            tc = plan.get("transfer_case", 1.0)
            tcline = ("Transfer-case ratio: %.3f (folded into the ratios)\n" % tc
                      if abs(tc - 1.0) > 1e-3 else "")
            phg = plan.get("placeholders") or []
            phline = ("Placeholder gears left untouched: %s\n"
                      % ", ".join(str(g) for g in phg) if phg else "")
            msg = ("Regenerate %d v/n band value(s) for \u2018%s\u2019 from the "
                   "absolute ratios?\n\nSource: %s\nRolling circumference: %.3f m\n"
                   "%s%s"
                   "The 1st-gear lower bound and the top real gear's upper bound "
                   "are kept as-is. One undoable step." %
                   (len(writes), copy.label, src, plan["circ"], tcline, phline))
            if QtWidgets.QMessageBox.question(
                    self, "Auto-generate V/N ratios", msg) \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            gr.apply_vn(writes)
            self.copies = gr.copies()
            self._populate()

        def _copy_changed(self, i):
            self.cur = self.copy_box.itemData(i) or 0
            self._populate()

        # -- v/n display-mode transforms -----------------------------------
        def _vn_kind(self):
            return self.vn_mode.currentData()

        def _apply_mode_ui(self):
            """Configure the reference spinbox for the current mode."""
            self.vn_ref.blockSignals(True)
            kind = self._vn_kind()
            if kind == "speed":
                self.vn_ref.setEnabled(True)
                self.vn_ref.setDecimals(0)
                self.vn_ref.setRange(100, 10000)
                self.vn_ref.setSingleStep(100)
                self.vn_ref.setSuffix(" rpm")
                self.vn_ref.setValue(self._last_rpm)
            elif kind == "rpm":
                self.vn_ref.setEnabled(True)
                self.vn_ref.setDecimals(0)
                self.vn_ref.setRange(1, 400)
                self.vn_ref.setSingleStep(5)
                self.vn_ref.setSuffix(" km/h")
                self.vn_ref.setValue(self._last_speed)
            else:                                   # raw ratio
                self.vn_ref.setEnabled(False)
                self.vn_ref.setSuffix("")
            self.vn_ref.blockSignals(False)
            self._update_unit_label()

        def _update_unit_label(self):
            kind = self._vn_kind()
            if kind == "speed":
                self.vn_unit_lbl.setText(
                    "km/h at %d rpm" % int(self.vn_ref.value()))
            elif kind == "rpm":
                self.vn_unit_lbl.setText(
                    "engine rpm at %d km/h" % int(self.vn_ref.value()))
            else:
                self.vn_unit_lbl.setText("(km/h) / rpm")

        def _vn_fmt(self, r):
            """Display string for a raw v/n ratio ``r`` under the current mode."""
            kind = self._vn_kind()
            if kind == "speed":
                return "%.1f" % (r * self.vn_ref.value())
            if kind == "rpm":
                if r <= 0:
                    return "-"                      # open band: rpm undefined
                return "%.0f" % (self.vn_ref.value() / r)
            return "%.4f" % r

        def _vn_parse(self, text):
            """Raw v/n ratio from an edited display value under the current
            mode (None if it cannot be converted)."""
            x = float(text)
            kind = self._vn_kind()
            if kind == "speed":
                return x / self.vn_ref.value()
            if kind == "rpm":
                if x <= 0:
                    return None
                return self.vn_ref.value() / x
            return x

        def _mode_changed(self, _i):
            self._apply_mode_ui()
            self._populate()

        def _ref_changed(self, _v):
            kind = self._vn_kind()
            if kind == "speed":
                self._last_rpm = self.vn_ref.value()
            elif kind == "rpm":
                self._last_speed = self.vn_ref.value()
            self._update_unit_label()
            self._populate()

        def _num_item(self, entity, name, shared, vn=False):
            """A cell holding one editable value. ``entity`` is the entity to
            edit for the selected copy (the copy's own, or the shared main-block
            one); ``name`` is the parameter symbol (used to mirror edits to
            other copies). ``vn`` applies the current v/n display transform."""
            if entity is None:
                it = QtWidgets.QTableWidgetItem("-")
                it.setFlags(QtCore.Qt.ItemIsEnabled)
                return it
            r = entity.cell(0, 0)
            text = self._vn_fmt(r) if vn else _fmt(r, _entity_decimals(entity))
            it = QtWidgets.QTableWidgetItem(text)
            it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            it.setData(QtCore.Qt.UserRole, entity)
            it.setData(QtCore.Qt.UserRole + 1, name)
            it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled
                        | QtCore.Qt.ItemIsEditable)
            if shared:
                it.setForeground(QtGui.QColor(140, 140, 140))
                it.setToolTip("%s @ 0x%X — shared main-block value (this copy "
                              "has no separate storage)" % (entity.name,
                                                            entity.addr))
            else:
                it.setToolTip("%s @ 0x%X" % (entity.name, entity.addr))
            return it

        def _label_item(self, text, tip=""):
            it = QtWidgets.QTableWidgetItem(text)
            it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            if tip:
                it.setToolTip(tip)
            return it

        def _cmp_copy(self):
            if not self.cmp_copies:
                return None
            return (self.cmp_copies[self.cur]
                    if self.cur < len(self.cmp_copies) else self.cmp_copies[0])

        def _cmp_value(self, name):
            cc = self._cmp_copy()
            if cc is None:
                return None
            ce, _sh = cc.entity(name, self.cmp_pbn)
            if ce is None:
                return None
            try:
                return ce.cell(0, 0)
            except Exception:
                return None

        def _val_diff(self, cv, e):
            if cv is None or e is None:
                return False
            try:
                return abs(cv - e.cell(0, 0)) > 1e-9
            except Exception:
                return False

        def _cmp_item(self, value, decimals, vn=False, diff=False):
            if value is None:
                it = QtWidgets.QTableWidgetItem("\u2014")
                it.setFlags(QtCore.Qt.ItemIsEnabled)
                return it
            text = self._vn_fmt(value) if vn else _fmt(value, decimals)
            it = QtWidgets.QTableWidgetItem(text)
            it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            if diff:
                it.setBackground(EDIT_BG)
                it.setForeground(CELL_DARK_FG)
            else:
                it.setForeground(QtGui.QColor(140, 140, 140))
            return it

        def _populate(self):
            self._loading = True
            pbn = self.pbn
            cp = self.copies[self.cur]
            arows = self.gears.abs_layout()
            self.abs_tbl.setRowCount(len(arows))
            for r, (lbl, name) in enumerate(arows):
                self.abs_tbl.setItem(r, 0, self._label_item(lbl, name))
                e, shared = cp.entity(name, pbn)
                self.abs_tbl.setItem(r, 1, self._num_item(e, name, shared))
                if self._has_cmp:
                    cv = self._cmp_value(name)
                    dec = _entity_decimals(e) if e is not None else 3
                    self.abs_tbl.setItem(r, 2, self._cmp_item(
                        cv, dec, diff=self._val_diff(cv, e)))

            # In RPM-at-speed mode the smaller v/n edge yields the LARGER rpm,
            # so the low/high columns invert. Swap which band edge feeds the
            # "Min"/"Max" columns so the left column is always the smaller
            # displayed value and the table still reads low -> high.
            swap = self._vn_kind() == "rpm"
            vrows = self.gears.vn_layout()
            self.vn_tbl.setRowCount(len(vrows))
            for r, (lbl, lo, hi) in enumerate(vrows):
                self.vn_tbl.setItem(r, 0, self._label_item(lbl))
                elo, slo = cp.entity(lo, pbn)
                ehi, shi = cp.entity(hi, pbn)
                (e1, s1, n1), (e2, s2, n2) = (
                    ((ehi, shi, hi), (elo, slo, lo)) if swap
                    else ((elo, slo, lo), (ehi, shi, hi)))
                self.vn_tbl.setItem(r, 1, self._num_item(e1, n1, s1, vn=True))
                self.vn_tbl.setItem(r, 2, self._num_item(e2, n2, s2, vn=True))
                if self._has_cmp:
                    cv1 = self._cmp_value(n1)
                    cv2 = self._cmp_value(n2)
                    self.vn_tbl.setItem(r, 3, self._cmp_item(
                        cv1, 0, vn=True, diff=self._val_diff(cv1, e1)))
                    self.vn_tbl.setItem(r, 4, self._cmp_item(
                        cv2, 0, vn=True, diff=self._val_diff(cv2, e2)))

            for tbl in (self.abs_tbl, self.vn_tbl):
                tbl.resizeColumnsToContents()
                tbl.horizontalHeader().setSectionResizeMode(
                    0, QtWidgets.QHeaderView.Stretch)
            self._loading = False

        def refresh_from_session(self):
            """Show edits made outside this window (the grid, undo, a patch,
            another tool) -- called by the tab while this window is open."""
            self._populate()

        def _edit_targets(self, name, entity):
            """Entities to write for an edit of parameter ``name`` in the
            selected copy: just that entity, or -- with 'copy to all' ticked --
            the matching entity in every copy, de-duplicated by address."""
            if not (self.copy_all.isChecked() and len(self.copies) > 1):
                return [entity]
            seen = set()
            out = []
            for cp in self.copies:
                e, _shared = cp.entity(name, self.pbn)
                if e is not None and e.addr not in seen:
                    seen.add(e.addr)
                    out.append(e)
            return out or [entity]

        def _changed(self, item):
            if self._loading or item is None:
                return
            entity = item.data(QtCore.Qt.UserRole)
            name = item.data(QtCore.Qt.UserRole + 1)
            if entity is None:
                return
            is_vn = item.tableWidget() is self.vn_tbl
            try:
                raw = self._vn_parse(item.text()) if is_vn \
                    else float(item.text())
            except (ValueError, ZeroDivisionError):
                self._populate()
                return
            if raw is None:
                self._populate()
                return
            for e in self._edit_targets(name, entity):
                self.gears.set_value(e, raw)
            self._populate()

        def _copy_gears_from_cmp(self):
            if not self._has_cmp:
                return
            cp = self.copies[self.cur]
            pbn = self.pbn
            names = [name for _lbl, name in self.gears.abs_layout()]
            for _lbl, lo, hi in self.gears.vn_layout():
                names += [lo, hi]
            pairs, seen = [], set()
            for name in names:
                cv = self._cmp_value(name)
                if cv is None:
                    continue
                base, _sh = cp.entity(name, pbn)
                for e in self._edit_targets(name, base):
                    if e is None or e.addr in seen:
                        continue
                    try:
                        if abs(e.cell(0, 0) - cv) <= 1e-9:
                            continue
                    except Exception:
                        pass
                    seen.add(e.addr)
                    pairs.append((e, cv))
            if not pairs:
                QtWidgets.QMessageBox.information(
                    self, "Copy from comparison",
                    "Nothing to copy \u2014 values already match, or the "
                    "comparison has no matching gear parameters.")
                return
            self.session.edit_values(
                pairs, "copy gear ratios from comparison")
            self.copies = self.gears.copies()
            self._populate()


    class HexModel(QtCore.QAbstractTableModel):
        """The whole binary as 16 bytes/row of editable hex plus an ASCII
        column. Edits route through the session (undoable, checksum-aware);
        bytes changed from the as-loaded image are highlighted."""
        COLS = 16

        def __init__(self, session, compare=None, extra_regions=None):
            super().__init__()
            self.session = session
            self.cmp = compare.binfile.data if compare is not None else None
            self.which = "primary"                   # or "compare"
            # Regions: External (live, editable) + any MPC/EEPROM the loaded
            # container carries (view-only here) + caller extras (e.g. EDC15 EEPROM).
            try:
                regs = list(session.region_buffers())
            except Exception:
                regs = [("External flash", bytes(session.binfile.data))]
            # extra_regions items are (name, buf) or (name, buf, writer);
            # a writer(off, bytes)->bool makes that region editable with the
            # edit routed to the caller (e.g. EDC15 EEPROM, owned by the
            # window). Session container regions (MPC / EEPROM) edit through
            # session.edit_region instead and need no writer here.
            self._region_writers = {}
            self._region_original = {}
            try:
                for _nm, _b in session._container_regions():
                    self._region_original[_nm] = _b
            except Exception:
                pass
            extra = []
            for _item in (extra_regions or []):
                if len(_item) == 3:
                    _nm, _buf, _wr = _item
                    self._region_writers[_nm] = _wr
                else:
                    _nm, _buf = _item
                self._region_original.setdefault(_nm, bytes(_buf))
                extra.append((_nm, _buf))
            self._regions = list(regs) + extra
            self.region = 0
            self.region_name = self._regions[0][0]
            self.region_writer = None
            self._build_regions()                    # entity spans (External only)
            self._apply_region()

        def region_names(self):
            return [n for n, _ in self._regions]

        def _apply_region(self):
            name, data = self._regions[self.region]
            self.region_name = name
            self.region_writer = None
            if self.region == 0:
                self.buf = self.session.binfile.data     # live external (editable)
                self.original = self.session.original
                self.editable = True
                return
            # A container region we can edit in place and fold back on save
            # (MPC / EEPROM): point the view at the session's live working
            # buffer so RegionCommand edits show immediately.
            live = None
            try:
                if self.session.region_editable(name):
                    live = self.session.region_edit_buffer(name)
            except Exception:
                live = None
            if live is not None:
                self.buf = live
                self.original = self._region_original.get(name, data)
                self.editable = True
                return
            # An extra region with its own writer callback (EDC15 EEPROM).
            wr = self._region_writers.get(name)
            if wr is not None:
                self.buf = data                          # the live bytearray we were handed
                self.original = self._region_original.get(name, bytes(data))
                self.editable = True
                self.region_writer = wr
                return
            self.buf = data                              # static view
            self.original = self._region_original.get(name, data)
            self.editable = False

        def set_region(self, idx):
            if idx == self.region or not (0 <= idx < len(self._regions)):
                return
            self.beginResetModel()
            self.region = idx
            self._apply_region()
            self.endResetModel()

        def shown(self):
            if self.which == "compare" and self.cmp is not None and self.region == 0:
                return self.cmp
            return self.buf

        def set_which(self, which):
            self.beginResetModel()
            self.which = which
            self.endResetModel()

        def _build_regions(self):
            """Sorted (start, end, name) spans of every defined parameter, for
            highlighting and hover in the hex view."""
            ivals = []
            try:
                ents = list(self.session.entities) \
                    + list(self.session.variant_entities())
            except Exception:
                ents = list(self.session.entities)
            for e in ents:
                span = e.n_cells * e.step
                if span > 0:
                    ivals.append((e.addr, e.addr + span, e.name))
            ivals.sort()
            self._ivals = ivals
            self._starts = [v[0] for v in ivals]

        def entity_at(self, a):
            """(name, start, end) of the defined parameter covering byte ``a``,
            or None. Bounded back-scan handles the occasional nested span."""
            if self.region != 0:
                return None                          # parameters are external-flash only
            import bisect
            i = bisect.bisect_right(self._starts, a) - 1
            steps = 0
            while i >= 0 and steps < 24:
                s, en, nm = self._ivals[i]
                if a < en:
                    return nm, s, en
                if s < a - 0x800:        # no span reaches this far back
                    break
                i -= 1
                steps += 1
            return None

        def rowCount(self, _=QtCore.QModelIndex()):
            return (len(self.shown()) + self.COLS - 1) // self.COLS

        def _differs(self, a):
            cmp_ = self.compare_shown()
            return (cmp_ is not None and a < len(self.buf)
                    and a < len(cmp_) and self.buf[a] != cmp_[a])

        def columnCount(self, _=QtCore.QModelIndex()):
            return self.COLS + 1                     # + ASCII

        def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
            if role != QtCore.Qt.DisplayRole:
                return None
            if orient == QtCore.Qt.Horizontal:
                return "ASCII" if sec == self.COLS else "%X" % sec
            return "%08X" % (sec * self.COLS)

        def _cell_bg(self, a):
            """Highlight colour for byte ``a`` (or None): differs-vs-comparison,
            edited-from-original, or inside a defined table."""
            if a >= len(self.buf):
                return None
            if self._differs(a):
                return HEX_DIFF_BG
            if self.compare_shown() is None and a < len(self.original) \
                    and self.original[a] != self.buf[a]:
                return HEX_EDIT_BG
            if self.entity_at(a):
                return HEX_DEF_BG
            return None

        def data(self, idx, role=QtCore.Qt.DisplayRole):
            if not idx.isValid():
                return None
            buf = self.shown()
            base = idx.row() * self.COLS
            col = idx.column()
            if col == self.COLS:                     # ASCII column
                if role == QtCore.Qt.DisplayRole:
                    chunk = buf[base:base + self.COLS]
                    return "".join(chr(c) if 32 <= c < 127 else "."
                                   for c in chunk)
                # highlight the ASCII run when the row opens a defined table
                bg = HEX_DEF_BG if self.entity_at(base) else None
                if role == QtCore.Qt.BackgroundRole:
                    return bg
                if role == QtCore.Qt.ForegroundRole:
                    return HEX_HL_FG if bg else QtGui.QColor(120, 120, 120)
                return None
            a = base + col
            if a >= len(buf):
                return None
            b = buf[a]
            if role in (QtCore.Qt.DisplayRole, QtCore.Qt.EditRole):
                return "%02X" % b
            if role == QtCore.Qt.TextAlignmentRole:
                return int(QtCore.Qt.AlignCenter)
            if role == QtCore.Qt.BackgroundRole:
                return self._cell_bg(a)
            if role == QtCore.Qt.ForegroundRole:
                # dark text on any highlight so it stays readable in both themes
                return HEX_HL_FG if self._cell_bg(a) else None
            if role == QtCore.Qt.ToolTipRole:
                reg = self.entity_at(a)
                base_tip = ""
                if reg:
                    nm, s, en = reg
                    base_tip = "%s  @0x%X  (0x%X..0x%X)" % (nm, a, s, en)
                cmp_ = self.compare_shown()
                if cmp_ is not None and a < len(cmp_) and a < len(self.buf):
                    d = ("primary %02X / comparison %02X%s"
                         % (self.buf[a], cmp_[a],
                            "  (differ)" if self.buf[a] != cmp_[a] else ""))
                    return base_tip + ("\n" if base_tip else "") + d
                return base_tip or None
            return None

        def flags(self, idx):
            f = super().flags(idx)
            # only the primary binary is editable; the comparison is read-only
            if self.can_write() and idx.column() < self.COLS \
                    and idx.row() * self.COLS + idx.column() < len(self.buf):
                f |= QtCore.Qt.ItemIsEditable
            return f

        def setData(self, idx, value, role=QtCore.Qt.EditRole):
            if role != QtCore.Qt.EditRole or idx.column() >= self.COLS \
                    or not self.can_write():
                return False
            try:
                b = int(str(value).strip(), 16) & 0xFF
            except (TypeError, ValueError):
                return False
            a = idx.row() * self.COLS + idx.column()
            if a >= len(self.buf):
                return False
            if not self.write(a, bytes([b])):
                return True                          # unchanged
            self.dataChanged.emit(idx, idx)
            asc = self.index(idx.row(), self.COLS)
            self.dataChanged.emit(asc, asc)
            return True

        def can_write(self):
            """True when the bytes on screen are the editable ones: the
            primary (not the comparison) and a region that takes edits."""
            return self.which == "primary" and bool(getattr(self, "editable", True))

        def write(self, a, data):
            """Write ``data`` at ``a`` into the region being SHOWN, routed the
            same way as a typed byte: main flash through the session,
            an MPC/EEPROM working buffer through ``edit_region``, a caller's
            region through its writer. True when something changed; False
            when nothing did or the shown bytes are not editable."""
            if not self.can_write():
                return False
            if self.region == 0:
                return self.session.edit_bytes(a, data) is not None
            if self.region_writer is not None:
                return bool(self.region_writer(a, data))
            return self.session.edit_region(self.region_name, a, data) is not None

        def compare_shown(self):
            """The comparison's bytes when they apply to the region on screen
            (the comparison is main flash only), else None."""
            return self.cmp if self.region == 0 else None

        def refresh(self):
            self.beginResetModel()
            self.endResetModel()


    class HexByteDelegate(QtWidgets.QStyledItemDelegate):
        """Makes the hex grid behave like a real hex editor: the editor accepts
        only hex, overtypes the current byte (starts with the value selected),
        caps at two digits, and once two are typed commits and jumps to the next
        byte so you can type a run of bytes without touching the mouse."""

        advance = QtCore.Signal()          # request: move to next byte + edit

        def createEditor(self, parent, option, index):
            ed = QtWidgets.QLineEdit(parent)
            rx = QtCore.QRegularExpression("[0-9A-Fa-f]{1,2}")
            ed.setValidator(QtGui.QRegularExpressionValidator(rx, ed))
            ed.setMaxLength(2)
            ed.setAlignment(QtCore.Qt.AlignCenter)
            ed.textEdited.connect(lambda t, e=ed: self._on_text(e, t))
            return ed

        def setEditorData(self, editor, index):
            # show the current byte, selected, so the first digit overtypes it
            editor.setText(str(index.data(QtCore.Qt.EditRole) or ""))
            editor.selectAll()

        def _on_text(self, editor, text):
            if len(text) >= 2:             # a full byte -> commit and advance
                QtCore.QTimer.singleShot(0, lambda: self._commit_advance(editor))

        def _commit_advance(self, editor):
            try:
                self.commitData.emit(editor)
                self.closeEditor.emit(
                    editor,
                    QtWidgets.QAbstractItemDelegate.EndEditHint.NoHint)
            except RuntimeError:
                return                     # editor already gone
            self.advance.emit()


    class HexDialog(QtWidgets.QDialog):
        """A hex editor over the loaded image: go-to-offset, find (hex or
        text), and modified-byte highlighting. Edits are undoable and are
        picked up by the parameter editor when the dialog closes."""

        def __init__(self, session: TunerSession, compare=None, parent=None,
                     extra_regions=None):
            super().__init__(parent)
            self.session = session
            self.compare = compare
            self._extra_regions = extra_regions
            self.setWindowTitle("Hex editor - %s" % session.binfile.name)
            self.setWindowIcon(app_icon())
            self.resize(760, 620)
            v = QtWidgets.QVBoxLayout(self)

            if compare is not None:
                sbar = QtWidgets.QHBoxLayout()
                sbar.addWidget(QtWidgets.QLabel("Show:"))
                self.show_box = QtWidgets.QComboBox()
                self.show_box.addItem("Primary — %s"
                                      % session.binfile.name, "primary")
                self.show_box.addItem("Comparison — %s"
                                      % compare.binfile.name, "compare")
                self.show_box.currentIndexChanged.connect(self._show_changed)
                sbar.addWidget(self.show_box)
                diff = QtWidgets.QLabel(
                    "Bytes that differ between the two binaries are highlighted; "
                    "only the primary is editable.")
                diff.setStyleSheet("color: gray")
                sbar.addWidget(diff)
                sbar.addStretch(1)
                v.addLayout(sbar)

            bar = QtWidgets.QHBoxLayout()
            bar.addWidget(QtWidgets.QLabel("Go to offset:"))
            self.goto = QtWidgets.QLineEdit()
            self.goto.setPlaceholderText("hex e.g. 1C0000")
            self.goto.setMaximumWidth(120)
            self.goto.returnPressed.connect(self._goto)
            bar.addWidget(self.goto)
            go = QtWidgets.QPushButton("Go")
            go.clicked.connect(self._goto)
            bar.addWidget(go)
            bar.addSpacing(16)
            bar.addWidget(QtWidgets.QLabel("Find:"))
            self.find = QtWidgets.QLineEdit()
            self.find.setPlaceholderText("hex bytes or text")
            self.find.returnPressed.connect(self._find_next)
            bar.addWidget(self.find)
            self.find_hex = QtWidgets.QCheckBox("hex")
            self.find_hex.setChecked(True)
            bar.addWidget(self.find_hex)
            nxt = QtWidgets.QPushButton("Find next")
            nxt.clicked.connect(self._find_next)
            bar.addWidget(nxt)
            v.addLayout(bar)

            # jump-to-table dropdown (defined parameters, type to filter)
            jbar = QtWidgets.QHBoxLayout()
            jbar.addWidget(QtWidgets.QLabel("Jump to table:"))
            self.jump = QtWidgets.QComboBox()
            self.jump.setEditable(True)
            self.jump.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
            self.jump.setMinimumWidth(360)
            # show the English label first, with the raw A2L symbol in
            # parentheses; sort by the English label so the list reads in
            # plain-language order.
            ents = []
            for e in (list(session.entities)
                      + list(session.variant_entities())):
                eng = _english(e)
                label = ("%s  (%s)" % (eng, e.name)
                         if eng != e.name else e.name)
                ents.append((eng.lower(), label, e.addr))
            ents.sort(key=lambda t: t[0])
            self.jump.addItem("", -1)
            self._jump_index = {}          # entity start addr -> combo index
            for _key, label, ad in ents:
                self._jump_index[ad] = self.jump.count()
                self.jump.addItem(label, ad)
            self.jump.completer().setCompletionMode(
                QtWidgets.QCompleter.PopupCompletion)
            self.jump.completer().setFilterMode(QtCore.Qt.MatchContains)
            self.jump.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.jump.activated.connect(self._jump_to)
            jbar.addWidget(self.jump)
            jbar.addStretch(1)
            v.addLayout(jbar)

            self.model = HexModel(session, compare, extra_regions=self._extra_regions)
            # Region switcher (External / MPC / EEPROM) -- shown when the loaded
            # source carries more than the external flash. External is editable;
            # MPC/EEPROM are read-only views here (export to edit, for now).
            _rnames = self.model.region_names()
            if len(_rnames) > 1:
                rbar = QtWidgets.QHBoxLayout()
                rbar.addWidget(QtWidgets.QLabel("Region:"))
                self.region_box = QtWidgets.QComboBox()
                for _i, _n in enumerate(_rnames):
                    self.region_box.addItem(_n, _i)
                self.region_box.currentIndexChanged.connect(self._region_changed)
                rbar.addWidget(self.region_box)
                self.region_note = QtWidgets.QLabel("")
                self.region_note.setStyleSheet("color: gray")
                rbar.addWidget(self.region_note)
                rbar.addStretch(1)
                v.addLayout(rbar)
            self.view = QtWidgets.QTableView()
            self.view.setModel(self.model)
            mono = QtGui.QFontDatabase.systemFont(
                QtGui.QFontDatabase.FixedFont)
            self.view.setFont(mono)
            self.view.horizontalHeader().setMinimumSectionSize(10)
            for c in range(HexModel.COLS):
                self.view.setColumnWidth(c, 30)
            self.view.setColumnWidth(HexModel.COLS, 190)
            self.view.verticalHeader().setDefaultSectionSize(20)
            self.view.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection)
            # type-to-edit like a hex editor: a hex key starts editing the byte
            self.view.setEditTriggers(
                QtWidgets.QAbstractItemView.DoubleClicked
                | QtWidgets.QAbstractItemView.EditKeyPressed
                | QtWidgets.QAbstractItemView.AnyKeyPressed)
            self._byte_delegate = HexByteDelegate(self.view)
            self._byte_delegate.advance.connect(self._advance_cursor)
            for c in range(HexModel.COLS):
                self.view.setItemDelegateForColumn(c, self._byte_delegate)

            def _hex_cols(f):
                for c in range(HexModel.COLS):
                    self.view.setColumnWidth(c, int(round(30 * f)))
                self.view.setColumnWidth(HexModel.COLS, int(round(190 * f)))
            from . import gui_zoom as _gz
            self._zoom = _gz.install(self.view, "hex", after=_hex_cols)
            self.view.selectionModel().currentChanged.connect(self._sel)
            v.addWidget(self.view)

            QtGui.QShortcut(QtGui.QKeySequence.Copy, self.view,
                            activated=self._copy)
            QtGui.QShortcut(QtGui.QKeySequence.Paste, self.view,
                            activated=self._paste)

            self.info = QtWidgets.QLabel("Offset -")
            self.info.setStyleSheet("color: gray")
            v.addWidget(self.info)

            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.accept)
            btns.addWidget(close)
            v.addLayout(btns)

        def refresh_from_session(self):
            """Show edits made outside this window (the grid, undo, a patch,
            another tool) -- called by the tab while this window is open."""
            self.model.refresh()

        def _addr_of(self, idx):
            if not idx.isValid():
                return None
            col = min(idx.column(), HexModel.COLS - 1)
            return idx.row() * HexModel.COLS + col

        def _select_addr(self, addr):
            addr = max(0, min(addr, len(self.model.shown()) - 1))
            idx = self.model.index(addr // HexModel.COLS,
                                   addr % HexModel.COLS)
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(
                idx, QtWidgets.QAbstractItemView.PositionAtCenter)

        def select_range(self, start, end):
            """Show bytes ``[start, end)`` of the external flash, selected
            and scrolled into view (the parameter tree's Show in hex
            editor): the primary image's flash region is brought up first."""
            if getattr(self, "region_box", None) is not None \
                    and self.region_box.currentIndex() != 0:
                self.region_box.setCurrentIndex(0)
            if getattr(self, "show_box", None) is not None \
                    and self.show_box.currentIndex() != 0:
                self.show_box.setCurrentIndex(0)
            n = len(self.model.shown())
            start = max(0, min(int(start), n - 1))
            end = max(start + 1, min(int(end), n))
            cols = HexModel.COLS
            sel = QtCore.QItemSelection()
            r0, r1 = start // cols, (end - 1) // cols
            for r in range(r0, r1 + 1):
                c0 = start % cols if r == r0 else 0
                c1 = (end - 1) % cols if r == r1 else cols - 1
                sel.select(self.model.index(r, c0), self.model.index(r, c1))
            sm = self.view.selectionModel()
            first = self.model.index(r0, start % cols)
            sm.setCurrentIndex(first, QtCore.QItemSelectionModel.NoUpdate)
            sm.select(sel, QtCore.QItemSelectionModel.ClearAndSelect)
            self.view.scrollTo(self.model.index(r1, (end - 1) % cols),
                               QtWidgets.QAbstractItemView.EnsureVisible)
            self.view.scrollTo(first, QtWidgets.QAbstractItemView.PositionAtTop
                               if r1 - r0 > 20 else
                               QtWidgets.QAbstractItemView.EnsureVisible)
            self.info.setText("0x%06X–0x%06X selected (%d bytes)"
                              % (start, end - 1, end - start))

        def _advance_cursor(self):
            """Move to the next byte and open its editor (called after a full
            byte is typed), wrapping across rows and skipping the ASCII column.
            Deferred so the just-closed editor is fully torn down first."""
            a = self._addr_of(self.view.currentIndex())
            if a is None or a + 1 >= len(self.model.shown()):
                return
            n = a + 1
            ni = self.model.index(n // HexModel.COLS, n % HexModel.COLS)

            def go():
                self.view.setCurrentIndex(ni)
                self.view.edit(ni)
            QtCore.QTimer.singleShot(0, go)

        def _goto(self):
            txt = self.goto.text().strip().lstrip("0x").lstrip("0X")
            if not txt:
                return
            try:
                self._select_addr(int(txt, 16))
            except ValueError:
                QtWidgets.QMessageBox.warning(self, "Go to offset",
                                              "Enter a hex offset.")

        def _pattern(self):
            txt = self.find.text().strip()
            if not txt:
                return None
            if self.find_hex.isChecked():
                try:
                    return bytes(int(h, 16) for h in txt.split())
                except ValueError:
                    return None
            return txt.encode("latin-1", "ignore")

        def _find_next(self):
            pat = self._pattern()
            if not pat:
                return
            start = (self._addr_of(self.view.currentIndex()) or -1) + 1
            buf = self.model.shown()        # the bytes on screen
            i = buf.find(pat, start)
            if i < 0:
                i = buf.find(pat, 0)        # wrap
            if i < 0:
                self.info.setText("Not found")
                return
            self._select_addr(i)

        def _show_changed(self, _i):
            a = self._addr_of(self.view.currentIndex())
            self.model.set_which(self.show_box.currentData())
            for c in range(HexModel.COLS):
                self.view.setColumnWidth(c, 30)
            self.view.setColumnWidth(HexModel.COLS, 190)
            if a is not None:
                self._select_addr(a)

        def _region_changed(self, _i):
            idx = self.region_box.currentData()
            self.model.set_region(idx)
            for c in range(HexModel.COLS):
                self.view.setColumnWidth(c, 30)
            self.view.setColumnWidth(HexModel.COLS, 190)
            editable = bool(getattr(self.model, "editable", idx == 0))
            if hasattr(self, "region_note"):
                if idx == 0:
                    self.region_note.setText("")
                elif editable:
                    self.region_note.setText("editable \u2014 saved with the rest of the set")
                else:
                    self.region_note.setText("view only \u2014 use Export to edit this region")
            if hasattr(self, "jump"):
                self.jump.setEnabled(idx == 0)   # defined tables are external-flash only

        def _jump_to(self, _i):
            ad = self.jump.currentData()
            if isinstance(ad, int) and ad >= 0:
                self._select_addr(ad)

        def _selected_addrs(self):
            """Sorted byte addresses of the selected hex cells (ASCII column
            ignored)."""
            out = []
            for ix in self.view.selectionModel().selectedIndexes():
                if ix.column() < HexModel.COLS:
                    a = ix.row() * HexModel.COLS + ix.column()
                    if a < len(self.model.shown()):
                        out.append(a)
            return sorted(out)

        def _copy(self):
            addrs = self._selected_addrs()
            if not addrs:
                return
            buf = self.model.shown()        # the bytes on screen
            text = " ".join("%02X" % buf[a] for a in addrs)
            QtWidgets.QApplication.clipboard().setText(text)
            self.info.setText("Copied %d byte(s) as hex" % len(addrs))

        def _paste(self):
            txt = QtWidgets.QApplication.clipboard().text().strip()
            if not txt:
                return
            cleaned = txt.replace("0x", " ").replace(",", " ")
            try:
                if " " in cleaned:
                    data = bytes(int(h, 16) for h in cleaned.split())
                else:                        # contiguous hex digits
                    data = bytes.fromhex(cleaned)
            except ValueError:
                self.info.setText("Clipboard is not valid hex")
                return
            start = self._addr_of(self.view.currentIndex())
            if start is None:
                return
            if not self.model.can_write():
                self.info.setText(
                    "Paste: the comparison is read-only" if self.model.which != "primary"
                    else "Paste: this region is view only")
                return
            # into the region on screen, routed like a typed byte
            if not self.model.write(start, data):
                self.info.setText("Paste: no change")
                return
            self.model.refresh()
            self._select_addr(start)
            self.info.setText("Pasted %d byte(s) at 0x%X" % (len(data), start))

        def _sel(self, cur, _prev):
            a = self._addr_of(cur)
            if a is None:
                return
            buf = self.model.shown()
            b = buf[a]
            extra = ""
            if a + 1 < len(buf):
                u16 = (b << 8) | buf[a + 1]
                extra = "  u16 %d (0x%04X)" % (u16, u16)
            reg = self.model.entity_at(a)
            where = "  in %s" % reg[0] if reg else ""
            cmp_ = ""
            _c = self.model.compare_shown()
            if _c is not None and a < len(_c) and a < len(self.model.buf):
                cmp_ = "   [primary %02X / comparison %02X%s]" % (
                    self.model.buf[a], _c[a],
                    ", differ" if self.model.buf[a] != _c[a] else "")
            self.info.setText("Offset 0x%X (%d)   byte %d (0x%02X)%s%s%s"
                              % (a, a, b, b, extra, where, cmp_))
            from . import gui_address           # the main window's too
            picked = {self._addr_of(i)
                      for i in self.view.selectionModel().selectedIndexes()
                      if i.column() < HexModel.COLS}    # (not the text)
            gui_address.show_hex(self, picked if len(picked) > 1 else [a])
            # reflect the covering table in the dropdown (blank if none).
            # setCurrentIndex does not emit 'activated', so this won't re-jump.
            idx = self._jump_index.get(reg[1], 0) if reg else 0
            if idx != self.jump.currentIndex():
                self.jump.blockSignals(True)
                self.jump.setCurrentIndex(idx)
                self.jump.blockSignals(False)


    class VssDialog(QtWidgets.QDialog):
        """Vehicle-speed (VSS) scaling, for both ECU generations.

        On **EDC16** it shows every ``VSSCD_ImpPerDist_C`` configuration (the
        active/primary one plus any coding-selected variant) with its pulses/km
        and signal source, and offers a pulses/km corrector (known count or a
        read-vs-actual error) plus a signal-source selector (hardware sensor vs
        CAN) applied to the primary.

        On **EDC15** the model is different: per-dataset FGG speed blocks
        (``fgwDA1``/``fgwDA2``, a {SF, TMX, IMP,...} struct) with the source in
        ``cowVAR_FGG``. It shows those blocks and offers a tyre-size rescale
        (which scales SF & TMX) plus a per-dataset source selector (hardware
        sensor / tachograph / CAN ABS / CAN cluster).

        Every write is undoable (Ctrl+Z); checksums are corrected on save."""

        def __init__(self, session: TunerSession, parent=None, compare=None):
            super().__init__(parent)
            self.session = session
            self._isvdo = getattr(session, "platform", "") == "vdo"
            self._is15 = bool(getattr(session, "is_edc15", False))
            self._compare = compare
            self._mainwin = parent
            self._cmp_rows16 = None
            self._cmp15 = None
            self._has_cmp = False
            if compare is not None and not self._isvdo:
                try:
                    if self._is15 and getattr(compare, "is_edc15", False):
                        from .edc15.vss15 import speed_settings as _e15s
                        self._cmp15 = _e15s(compare)
                        self._has_cmp = True
                    elif not self._is15 \
                            and not getattr(compare, "is_edc15", False):
                        self._cmp_rows16 = vss_pulse_settings(compare)
                        self._has_cmp = True
                except Exception:
                    self._has_cmp = False
            self.setWindowTitle("VSS pulse settings")
            self.setWindowIcon(app_icon())
            self._v = QtWidgets.QVBoxLayout(self)
            if self._isvdo:
                self.setWindowTitle("Speed & distance (cluster)")
                self.resize(560, 560)
                self._build_vdo()
            elif self._is15:
                self.resize(620, 560)
                self._build_edc15()
            else:
                self.resize(460, 520)
                self._build_edc16()
            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.accept)
            btns.addWidget(close)
            self._v.addLayout(btns)
            self._reload()

        def _reload(self):
            (self._reload_vdo if self._isvdo else
             self._reload15 if self._is15 else self._reload16)()

        def refresh_from_session(self):
            """Show edits made outside this window (the grid, undo, a patch,
            another tool) -- called by the tab while this window is open."""
            self._reload()

        def _after_write(self):
            if self.parent() is not None:
                self.parent()._status()

        def _desc(self, html):
            """A wrapping description label that does NOT force the dialog
            wide (ignores its own width hint and wraps to the dialog)."""
            lbl = QtWidgets.QLabel(html)
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                              QtWidgets.QSizePolicy.Policy.Minimum)
            return lbl

        # ============================ EDC16 =========================
        def _build_edc16(self):
            v = self._v
            v.addWidget(self._desc(
                "<b>Configurations</b> &mdash; <code>VSSCD_ImpPerDist_C</code>, "
                "the speed-sensor pulses the ECU expects per kilometre. "
                "Double-click <b>Imp/m</b> or <b>Pulses/km</b> to edit any row "
                "(primary or variant); pick the <b>signal source</b> from its "
                "drop-down. <i>v</i> = 3.6 &times; <i>f</i> / (pulses-per-km / "
                "1000)."))
            _cols = ["Role", "Address", "Imp / m", "Pulses / km",
                     "Signal source"]
            if self._has_cmp:
                _cols += ["Pulses/km (cmp)", "Source (cmp)"]
            self._vss16_cols = _cols
            self.tbl = QtWidgets.QTableWidget(0, len(_cols))
            self.tbl.setHorizontalHeaderLabels(_cols)
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.horizontalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.tbl.itemChanged.connect(self._cell_changed16)
            if self._has_cmp:
                self.tbl.setContextMenuPolicy(
                    QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
                self.tbl.customContextMenuRequested.connect(self._vss_menu16)
            v.addWidget(self.tbl)

            cav = QtWidgets.QHBoxLayout()
            self.copy_all_btn = QtWidgets.QPushButton(
                "Copy primary settings to all variants")
            self.copy_all_btn.clicked.connect(self._copy_to_variants16)
            cav.addWidget(self.copy_all_btn)
            if self._has_cmp:
                self.copy_cmp_btn = QtWidgets.QPushButton(
                    "Copy comparison \u2192 primary")
                self.copy_cmp_btn.setToolTip(
                    "Copy the comparison binary's pulses/km (and source) into "
                    "the matching row(s).")
                self.copy_cmp_btn.clicked.connect(self._copy_from_cmp16)
                cav.addWidget(self.copy_cmp_btn)
            cav.addStretch(1)
            v.addLayout(cav)

            box = QtWidgets.QGroupBox("Read-vs-actual corrector (primary)")
            gv = QtWidgets.QVBoxLayout(box)
            er = QtWidgets.QHBoxLayout()
            er.addWidget(QtWidgets.QLabel("Speedo reads:"))
            self.read = QtWidgets.QDoubleSpinBox()
            self.read.setRange(1.0, 400.0)
            self.read.setDecimals(1)
            self.read.setSuffix(" km/h")
            self.read.valueChanged.connect(self._recalc)
            er.addWidget(self.read)
            er.addWidget(QtWidgets.QLabel("Actual:"))
            self.actual = QtWidgets.QDoubleSpinBox()
            self.actual.setRange(1.0, 400.0)
            self.actual.setDecimals(1)
            self.actual.setSuffix(" km/h")
            self.actual.valueChanged.connect(self._recalc)
            er.addWidget(self.actual)
            er.addStretch(1)
            self.apply_btn = QtWidgets.QPushButton("Apply to primary")
            self.apply_btn.clicked.connect(self._apply)
            er.addWidget(self.apply_btn)
            gv.addLayout(er)
            self.result = QtWidgets.QLabel()
            self.result.setWordWrap(True)
            gv.addWidget(self.result)
            v.addWidget(box)

            note = self._desc(
                "Editing a cell writes that row's 4-byte pulses/km. Choosing a "
                "source writes <code>VSSCD_swtSigVal_C</code> and forces "
                "<code>VSSCD_swtSigType_C</code>=2 so the calibrated source is "
                "used; a variant that shares the primary's source has no "
                "separate switch and its drop-down is disabled. Everything is "
                "undoable (Ctrl+Z); checksums are fixed on save.")
            note.setStyleSheet("color: gray")
            v.addWidget(note)

        def _vss_src_entities(self):
            ents = {e.name: e for e in list(self.session.entities)
                    + list(self.session.variant_entities())}
            return ents.get("VSSCD_swtSigVal_C"), ents.get("VSSCD_swtSigType_C")

        def _primary(self):
            for r in vss_pulse_settings(self.session):
                if r["primary"]:
                    return r
            return None

        def _reload16(self):
            self._loading = True
            rows = vss_pulse_settings(self.session)
            self._rows16 = rows
            self._src_combos = []
            self.tbl.setRowCount(len(rows))
            for i, r in enumerate(rows):
                role = QtWidgets.QTableWidgetItem(
                    "primary" if r["primary"] else "variant")
                role.setFlags(QtCore.Qt.ItemIsEnabled)
                self.tbl.setItem(i, 0, role)
                ad = QtWidgets.QTableWidgetItem("0x%X" % r["addr"])
                ad.setFlags(QtCore.Qt.ItemIsEnabled)
                ad.setFont(QtGui.QFont("Consolas"))
                self.tbl.setItem(i, 1, ad)
                for col, txt in ((2, "%.3f" % r["imp_m"]),
                                 (3, "%d" % r["imp_km"])):
                    it = QtWidgets.QTableWidgetItem(txt)
                    it.setFont(QtGui.QFont("Consolas"))
                    it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsEditable
                                | QtCore.Qt.ItemIsSelectable)
                    self.tbl.setItem(i, col, it)
                # source drop-down (editable where the row has its own switch);
                # short labels keep the column narrow, full meaning in tooltip
                combo = QtWidgets.QComboBox()
                combo.addItem("Sensor", 0)
                combo.addItem("CAN", 1)
                combo.setMaximumWidth(96)
                sval = r.get("src_val")
                combo.setCurrentIndex(1 if sval == 1 else 0)
                combo.setEnabled(r.get("src_addr") is not None)
                if r.get("src_addr") is None:
                    combo.setToolTip("This variant follows the primary's "
                                     "source (no separate switch byte).")
                else:
                    combo.setToolTip("Sensor = hardware hall/gearbox sensor;  "
                                     "CAN = speed over CAN (ABS / gateway)")
                combo.currentIndexChanged.connect(
                    lambda _ix, row=i: self._source_changed16(row))
                self._src_combos.append(combo)
                self.tbl.setCellWidget(i, 4, combo)
                if self._has_cmp:
                    cr = (self._cmp_rows16[i]
                          if i < len(self._cmp_rows16) else None)
                    kmit = QtWidgets.QTableWidgetItem(
                        "%d" % cr["imp_km"] if cr else "\u2014")
                    kmit.setFont(QtGui.QFont("Consolas"))
                    kmit.setFlags(QtCore.Qt.ItemIsEnabled
                                  | QtCore.Qt.ItemIsSelectable)
                    srcit = QtWidgets.QTableWidgetItem(
                        cr["src"] if cr else "\u2014")
                    srcit.setFlags(QtCore.Qt.ItemIsEnabled
                                   | QtCore.Qt.ItemIsSelectable)
                    if cr and cr["imp_km"] != r["imp_km"]:
                        kmit.setBackground(EDIT_BG)
                        kmit.setForeground(CELL_DARK_FG)
                    if cr and cr.get("src") != r.get("src"):
                        srcit.setBackground(EDIT_BG)
                        srcit.setForeground(CELL_DARK_FG)
                    self.tbl.setItem(i, 5, kmit)
                    self.tbl.setItem(i, 6, srcit)
            self.copy_all_btn.setEnabled(len(rows) > 1)
            p = self._primary()
            if p is not None and self.actual.value() < 2:
                self.read.setValue(100.0)
                self.actual.setValue(100.0)
            self._loading = False
            self._recalc()

        def _write_pulses16(self, row, km):
            raw = round(3.6e9 / (km / 1000.0)) if km else 0
            if not (0 <= raw <= 0xFFFFFFFF):
                QtWidgets.QMessageBox.warning(
                    self, "VSS",
                    "That pulses/km value is outside the storable range.")
                self._reload()
                return
            self.session.edit_bytes(self._rows16[row]["addr"],
                                    raw.to_bytes(4, "big"))
            self._reload()
            self._after_write()

        def _cell_changed16(self, item):
            if getattr(self, "_loading", False):
                return
            row, col = item.row(), item.column()
            try:
                x = float(item.text().strip())
            except ValueError:
                self._reload()
                return
            if col == 2:                       # Imp/m -> km = imp_m*1000
                self._write_pulses16(row, x * 1000.0)
            elif col == 3:                     # Pulses/km
                self._write_pulses16(row, x)

        def _source_changed16(self, row):
            if getattr(self, "_loading", False):
                return
            r = self._rows16[row]
            saddr = r.get("src_addr")
            if saddr is None:
                return
            want = int(self._src_combos[row].currentData())
            data = self.session.binfile.data
            changed = False
            # one action for the user -> one undo step
            with self.session.transaction("VSS source selection"):
                if 0 <= saddr < len(data) and data[saddr] != want:
                    self.session.edit_bytes(saddr, bytes([want]))
                    changed = True
                styp = r.get("styp_addr")       # force cal-controlled source
                if styp is not None and 0 <= styp < len(data) and data[styp] != 2:
                    self.session.edit_bytes(styp, bytes([2]))
                    changed = True
            if changed:
                self._reload()
                self._after_write()

        def _copy_to_variants16(self):
            rows = getattr(self, "_rows16", [])
            p = next((r for r in rows if r["primary"]), None)
            if p is None:
                return
            data = self.session.binfile.data
            praw = p["raw"].to_bytes(4, "big")
            n = 0
            with self.session.transaction("VSS: copy primary to variants"):
                for r in rows:
                    if r["primary"]:
                        continue
                    if data[r["addr"]:r["addr"] + 4] != praw:
                        self.session.edit_bytes(r["addr"], praw)
                        n += 1
                    sa = r.get("src_addr")
                    if sa is not None and p.get("src_val") is not None \
                            and 0 <= sa < len(data) and data[sa] != p["src_val"]:
                        self.session.edit_bytes(sa, bytes([p["src_val"]]))
                        n += 1
            if n:
                self._reload()
                self._after_write()

        def _copy_from_cmp16(self, only_rows=None):
            if not self._has_cmp or self._cmp_rows16 is None:
                return
            rows = getattr(self, "_rows16", [])
            data = self.session.binfile.data
            n = 0
            idxs = (list(only_rows) if only_rows is not None
                    else range(len(rows)))
            for i in idxs:
                if i >= len(rows) or i >= len(self._cmp_rows16):
                    continue
                r = rows[i]
                cr = self._cmp_rows16[i]
                craw = int(cr["raw"]).to_bytes(4, "big")
                if data[r["addr"]:r["addr"] + 4] != craw:
                    self.session.edit_bytes(r["addr"], craw)
                    n += 1
                sa = r.get("src_addr")
                cv = cr.get("src_val")
                if sa is not None and cv is not None \
                        and 0 <= sa < len(data) and data[sa] != cv:
                    self.session.edit_bytes(sa, bytes([cv]))
                    n += 1
                    styp = r.get("styp_addr")
                    if styp is not None and 0 <= styp < len(data) \
                            and data[styp] != 2:
                        self.session.edit_bytes(styp, bytes([2]))
            if n:
                self._reload()
                self._after_write()

        def _vss_menu16(self, pos):
            if not self._has_cmp:
                return
            rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
            if not rows:
                return
            menu = QtWidgets.QMenu(self)
            act = menu.addAction(
                "Copy comparison \u2192 primary (%d row(s))" % len(rows))
            act.triggered.connect(lambda: self._copy_from_cmp16(rows))
            menu.exec(self.tbl.viewport().mapToGlobal(pos))

        def _recalc(self, *a):
            if getattr(self, "_loading", False):
                return
            p = self._primary()
            if p is None:
                self.result.setText("")
                self.apply_btn.setEnabled(False)
                return
            actual = self.actual.value()
            km = p["imp_km"] * (self.read.value() / actual) if actual else None
            if km is None:
                self.apply_btn.setEnabled(False)
                return
            imp_m = km / 1000.0
            raw = round(3.6e9 / imp_m) if imp_m else 0
            ok = 0 < raw <= 0xFFFFFFFF
            self.apply_btn.setEnabled(ok and round(km) != p["imp_km"])
            msg = ("Would set primary to <b>%d</b> pulses/km (%.3f Imp/m) "
                   "&mdash; was %d" % (round(km), imp_m, p["imp_km"]))
            self.result.setText(msg)

        def _apply(self):
            p = self._primary()
            if p is None:
                return
            actual = self.actual.value()
            if actual <= 0:
                return
            km = p["imp_km"] * (self.read.value() / actual)
            raw = round(3.6e9 / (km / 1000.0))
            if not (0 < raw <= 0xFFFFFFFF):
                return
            self.session.edit_bytes(p["addr"], raw.to_bytes(4, "big"))
            self._reload()
            self._after_write()

        # ======================= VDO cluster =======================
        def _build_vdo(self):
            v = self._v
            v.addWidget(self._desc(
                "<b>Instrument cluster</b> &mdash; the <b>K-value</b> (impulses "
                "per km) sets both the speedometer and the odometer. The "
                "<b>speedometer needle map</b> sets only the needle. Every "
                "change is undoable (Ctrl+Z); the gauge checksum is fixed on "
                "save."))
            self.vdo_state = QtWidgets.QLabel()
            self.vdo_state.setWordWrap(True)
            self.vdo_state.setTextFormat(QtCore.Qt.RichText)
            v.addWidget(self.vdo_state)

            box = QtWidgets.QGroupBox("Set the K-value (speedometer and odometer)")
            gv = QtWidgets.QVBoxLayout(box)
            mrow = QtWidgets.QHBoxLayout()
            mrow.addWidget(QtWidgets.QLabel("From:"))
            self.vdo_mode = QtWidgets.QComboBox()
            self.vdo_mode.addItem("Tyre size and sender pulses", "tyre")
            self.vdo_mode.addItem("Tyre size change", "change")
            self.vdo_mode.addItem("Displayed vs actual speed", "speed")
            self.vdo_mode.addItem("Odometer vs actual distance", "dist")
            self.vdo_mode.currentIndexChanged.connect(self._vdo_mode_changed)
            mrow.addWidget(self.vdo_mode, 1)
            gv.addLayout(mrow)

            def _row(*widgets):
                w = QtWidgets.QWidget()
                h = QtWidgets.QHBoxLayout(w)
                h.setContentsMargins(0, 0, 0, 0)
                for x in widgets:
                    if isinstance(x, str):
                        x = QtWidgets.QLabel(x)
                    h.addWidget(x)
                h.addStretch(1)
                gv.addWidget(w)
                return w

            self.vdo_tyre = QtWidgets.QLineEdit("195/65R15")
            self.vdo_tyre.setMaximumWidth(110)
            self.vdo_pulses = QtWidgets.QDoubleSpinBox()
            self.vdo_pulses.setRange(1, 200)
            self.vdo_pulses.setDecimals(0)
            self.vdo_pulses.setToolTip("Gearbox / ABS sender pulses per wheel "
                                       "revolution: 7 on Golf / Jetta / Bora, "
                                       "8 on Passat and most others.")
            self.vdo_w_tyre = _row("Tyre:", self.vdo_tyre, "Pulses per wheel turn:",
                                   self.vdo_pulses)
            self.vdo_old = QtWidgets.QLineEdit("195/65R15")
            self.vdo_old.setMaximumWidth(110)
            self.vdo_new = QtWidgets.QLineEdit("205/55R16")
            self.vdo_new.setMaximumWidth(110)
            self.vdo_w_change = _row("Original tyre:", self.vdo_old, "New tyre:",
                                     self.vdo_new)
            self.vdo_shown = QtWidgets.QDoubleSpinBox()
            self.vdo_actual = QtWidgets.QDoubleSpinBox()
            for sp in (self.vdo_shown, self.vdo_actual):
                sp.setRange(0.1, 100000.0)
                sp.setDecimals(1)
                sp.setValue(100.0)
            self.vdo_shown_lbl = QtWidgets.QLabel("Displayed:")
            self.vdo_actual_lbl = QtWidgets.QLabel("Actual (GPS):")
            self.vdo_w_meas = _row(self.vdo_shown_lbl, self.vdo_shown,
                                   self.vdo_actual_lbl, self.vdo_actual,
                                   "(same units)")
            for w in (self.vdo_tyre, self.vdo_old, self.vdo_new):
                w.textChanged.connect(self._vdo_recalc)
            for w in (self.vdo_pulses, self.vdo_shown, self.vdo_actual):
                w.valueChanged.connect(self._vdo_recalc)
            self.vdo_result = QtWidgets.QLabel()
            self.vdo_result.setWordWrap(True)
            gv.addWidget(self.vdo_result)
            ab = QtWidgets.QHBoxLayout()
            ab.addStretch(1)
            self.vdo_apply_k = QtWidgets.QPushButton("Apply K-value")
            self.vdo_apply_k.clicked.connect(self._vdo_apply_kvalue)
            ab.addWidget(self.vdo_apply_k)
            gv.addLayout(ab)
            v.addWidget(box)

            nb = QtWidgets.QGroupBox("Correct the speedometer needle only "
                                     "(odometer unchanged)")
            nv = QtWidgets.QVBoxLayout(nb)
            nrow = QtWidgets.QHBoxLayout()
            self.vdo_n_shown = QtWidgets.QDoubleSpinBox()
            self.vdo_n_actual = QtWidgets.QDoubleSpinBox()
            for sp in (self.vdo_n_shown, self.vdo_n_actual):
                sp.setRange(1.0, 400.0)
                sp.setDecimals(1)
                sp.setValue(100.0)
                sp.valueChanged.connect(self._vdo_recalc)
            nrow.addWidget(QtWidgets.QLabel("Needle shows:"))
            nrow.addWidget(self.vdo_n_shown)
            nrow.addWidget(QtWidgets.QLabel("Actual:"))
            nrow.addWidget(self.vdo_n_actual)
            nrow.addStretch(1)
            self.vdo_apply_n = QtWidgets.QPushButton("Apply to needle map")
            self.vdo_apply_n.clicked.connect(self._vdo_apply_needle)
            nrow.addWidget(self.vdo_apply_n)
            nv.addLayout(nrow)
            self.vdo_n_result = QtWidgets.QLabel()
            self.vdo_n_result.setWordWrap(True)
            nv.addWidget(self.vdo_n_result)
            v.addWidget(nb)
            self._vdo_mode_changed()

        def _vdo_mode_changed(self, *a):
            mode = self.vdo_mode.currentData()
            self.vdo_w_tyre.setVisible(mode == "tyre")
            self.vdo_w_change.setVisible(mode == "change")
            self.vdo_w_meas.setVisible(mode in ("speed", "dist"))
            dist = mode == "dist"
            self.vdo_shown_lbl.setText("Odometer:" if dist else "Displayed:")
            self.vdo_actual_lbl.setText("Actual distance:" if dist else "Actual (GPS):")
            self._vdo_recalc()

        def _reload_vdo(self):
            from .vdo import speed as _vs
            k = _vs.k_value(self.session)
            ident = getattr(self.session, "vdo_identity", None)
            fb = getattr(ident, "family_byte", None)
            cod = next((e for e in self.session.entities
                        if e.name == _vs.CODING_NAME), None)
            codtxt = ""
            if cod is not None:
                raw = int(cod.cell(0, 0))
                codtxt = "%d &mdash; %s" % (raw, cod.options.get(raw, "?"))
            if not getattr(self, "_vdo_pulses_set", False):
                self.vdo_pulses.setValue(_vs.default_pulses_per_rev(fb))
                self._vdo_pulses_set = True
            lines = ["K-value: <b>%s</b> impulses/km" % ("?" if k is None else k)]
            if codtxt:
                lines.append("Coding digit 5: %s" % codtxt)
            if fb is not None:
                lines.append("Chassis: %s &mdash; %d pulses per wheel turn by default"
                             % (_vs.chassis_note(fb), _vs.default_pulses_per_rev(fb)))
            lines += ["<i>%s</i>" % w for w in _vs.warnings(self.session)]
            self.vdo_state.setText("<br>".join(lines))
            self._vdo_recalc()

        def _vdo_target(self):
            """(new K, explanation) for the chosen method, or (None, why)."""
            from .vdo import speed as _vs
            k = _vs.k_value(self.session)
            if k is None:
                return None, "No K-value in this cluster definition."
            mode = self.vdo_mode.currentData()
            try:
                if mode == "tyre":
                    circ = _vs.tyre_circumference_mm(self.vdo_tyre.text())
                    nk = _vs.k_from_tyre(self.vdo_tyre.text(), self.vdo_pulses.value())
                    why = ("%s: %.0f mm round, %.1f turns/km x %d pulses"
                           % (self.vdo_tyre.text().strip(), circ, 1.0e6 / circ,
                              self.vdo_pulses.value()))
                elif mode == "change":
                    nk = _vs.k_for_tyre_change(k, self.vdo_old.text(), self.vdo_new.text())
                    why = "circumference ratio %.4f" % (
                        _vs.tyre_circumference_mm(self.vdo_old.text())
                        / _vs.tyre_circumference_mm(self.vdo_new.text()))
                else:
                    nk = _vs.k_from_measured(k, self.vdo_shown.value(),
                                             self.vdo_actual.value())
                    why = "reads %+.1f%%" % ((self.vdo_shown.value()
                                             / self.vdo_actual.value() - 1) * 100)
            except ValueError as ex:
                return None, str(ex)
            return nk, why

        def _vdo_recalc(self, *a):
            if not hasattr(self, "vdo_result"):
                return
            from .vdo import speed as _vs
            k = _vs.k_value(self.session)
            nk, why = self._vdo_target()
            if nk is None:
                self.vdo_result.setText(why)
                self.vdo_apply_k.setEnabled(False)
            else:
                ok = 0 < nk <= 0xFFFF
                self.vdo_result.setText(
                    "Would set the K-value to <b>%d</b> (was %s) &mdash; %s%s"
                    % (nk, k, why, "" if ok else " &mdash; does not fit in 16 bits"))
                self.vdo_apply_k.setEnabled(ok and nk != k)
            ns, na = self.vdo_n_shown.value(), self.vdo_n_actual.value()
            spd = next((e for e in self.session.entities
                        if e.name == _vs.SPEEDO_NAME), None)
            if spd is None:
                self.vdo_n_result.setText("No speedometer map in this definition.")
                self.vdo_apply_n.setEnabled(False)
                return
            vals = spd.values()
            which = "speed points" if _vs.has_slope_break(vals[0], vals[1]) \
                else "needle positions"
            self.vdo_n_result.setText(
                "Scales the speedometer map's %s by %.4f (reads %+.1f%%).%s"
                % (which, na / ns, (ns / na - 1) * 100,
                   " This map has a slope break (Highline face), so the speed "
                   "points move instead of the needle positions."
                   if which == "speed points" else ""))
            self.vdo_apply_n.setEnabled(abs(ns - na) > 1e-9)

        def _vdo_apply_kvalue(self):
            from .vdo import speed as _vs
            nk, why = self._vdo_target()
            if nk is None:
                return
            try:
                _vs.set_k_value(self.session, nk, "K-value (%s)" % why)
            except ValueError as ex:
                QtWidgets.QMessageBox.warning(self, "K-value", str(ex))
                return
            self._reload()
            self._after_write()

        def _vdo_apply_needle(self):
            from .vdo import speed as _vs
            try:
                _vs.apply_needle_correction(self.session, self.vdo_n_shown.value(),
                                            self.vdo_n_actual.value())
            except ValueError as ex:
                QtWidgets.QMessageBox.warning(self, "Speedometer", str(ex))
                return
            self.vdo_n_shown.setValue(self.vdo_n_actual.value())
            self._reload()
            self._after_write()

        # ============================ EDC15 =========================
        def _build_edc15(self):
            from .edc15.vss15 import FGG_SOURCES as _FGG_SRC
            v = self._v
            v.addWidget(self._desc(
                "<b>EDC15 FGG speed blocks</b> &mdash; per dataset "
                "(<code>fgwDA1</code>/<code>fgwDA2</code>), the distance factor "
                "<code>SF</code>, ceiling <code>TMX</code> and sensor pulses/rev "
                "(<code>IMP</code>/60). The active block is selected by "
                "<code>cowFUN_FGG</code> and the source by "
                "<code>cowVAR_FGG</code>."))
            _cols15 = ["Dataset", "Block", "Source", "Active", "SF", "TMX",
                       "Pulses/rev"]
            if self._has_cmp:
                _cols15 += ["SF (cmp)", "TMX (cmp)", "Pulses/rev (cmp)"]
            self._vss15_cols = _cols15
            self.tbl = QtWidgets.QTableWidget(0, len(_cols15))
            self.tbl.setHorizontalHeaderLabels(_cols15)
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.tbl.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
            self.tbl.horizontalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.Stretch)
            v.addWidget(self.tbl)

            # "&&": a single & in a group-box title is a mnemonic marker
            box = QtWidgets.QGroupBox("Speed correction — rescales SF && TMX "
                                      "in every dataset")
            gv = QtWidgets.QVBoxLayout(box)
            mrow = QtWidgets.QHBoxLayout()
            mrow.addWidget(QtWidgets.QLabel("Method:"))
            self.corr_mode = QtWidgets.QComboBox()
            self.corr_mode.addItem("Tyre size change", "tyre")
            self.corr_mode.addItem("Measured speed (actual vs indicated)", "speed")
            self.corr_mode.currentIndexChanged.connect(self._corr_mode_changed)
            mrow.addWidget(self.corr_mode)
            mrow.addStretch(1)
            gv.addLayout(mrow)
            # tyre-size inputs
            self.tyre_w = QtWidgets.QWidget()
            trow = QtWidgets.QHBoxLayout(self.tyre_w)
            trow.setContentsMargins(0, 0, 0, 0)
            trow.addWidget(QtWidgets.QLabel("Original tyre:"))
            self.tyre_old = QtWidgets.QLineEdit("195/65R15")
            self.tyre_old.textChanged.connect(self._recalc15)
            trow.addWidget(self.tyre_old)
            trow.addWidget(QtWidgets.QLabel("New tyre:"))
            self.tyre_new = QtWidgets.QLineEdit("205/55R16")
            self.tyre_new.textChanged.connect(self._recalc15)
            trow.addWidget(self.tyre_new)
            gv.addWidget(self.tyre_w)
            # measured-speed inputs
            self.speed_w = QtWidgets.QWidget()
            srow = QtWidgets.QHBoxLayout(self.speed_w)
            srow.setContentsMargins(0, 0, 0, 0)
            srow.addWidget(QtWidgets.QLabel("Speedo indicates:"))
            self.spd_reported = QtWidgets.QLineEdit("105")
            self.spd_reported.textChanged.connect(self._recalc15)
            srow.addWidget(self.spd_reported)
            srow.addWidget(QtWidgets.QLabel("Actual (GPS):"))
            self.spd_actual = QtWidgets.QLineEdit("100")
            self.spd_actual.textChanged.connect(self._recalc15)
            srow.addWidget(self.spd_actual)
            srow.addWidget(QtWidgets.QLabel("(same units)"))
            gv.addWidget(self.speed_w)
            self.ktg_chk = QtWidgets.QCheckBox(
                "Also scale tachograph self-learn bounds (fgwKTG)")
            gv.addWidget(self.ktg_chk)
            self.result15 = QtWidgets.QLabel()
            self.result15.setWordWrap(True)
            gv.addWidget(self.result15)
            ab = QtWidgets.QHBoxLayout()
            ab.addStretch(1)
            self.apply15 = QtWidgets.QPushButton("Apply correction")
            self.apply15.clicked.connect(self._apply_rescale15)
            ab.addWidget(self.apply15)
            gv.addLayout(ab)
            v.addWidget(box)
            self._corr_mode_changed()

            # -- per-dataset source selector --
            sb = QtWidgets.QGroupBox("Signal source (per dataset)")
            sv = QtWidgets.QHBoxLayout(sb)
            sv.addWidget(QtWidgets.QLabel("Dataset:"))
            self.ds_box = QtWidgets.QComboBox()
            self._cowvar = {e.group: e for e in self.session.entities
                            if e.name == "cowVAR_FGG"}
            for label in sorted(self._cowvar):
                self.ds_box.addItem(label, label)
            self.ds_box.currentIndexChanged.connect(self._ds_changed15)
            sv.addWidget(self.ds_box)
            sv.addWidget(QtWidgets.QLabel("Source:"))
            self.src15_box = QtWidgets.QComboBox()
            for code in sorted(_FGG_SRC):
                self.src15_box.addItem(_FGG_SRC[code], code)
            sv.addWidget(self.src15_box)
            sv.addStretch(1)
            self.src15_btn = QtWidgets.QPushButton("Set source")
            self.src15_btn.clicked.connect(self._apply_source15)
            sv.addWidget(self.src15_btn)
            sb.setEnabled(bool(self._cowvar))
            v.addWidget(sb)

            note = self._desc(
                "Tyre rescale multiplies SF and TMX by (new circumference / old) "
                "in every dataset. The speed corrector does the same by "
                "actual&nbsp;/&nbsp;indicated &mdash; use it to true up a speedo "
                "that reads wrong for any reason (indicated speed is proportional "
                "to SF). Setting the source writes <code>cowVAR_FGG</code> for "
                "the chosen dataset. All are undoable (Ctrl+Z); checksums are "
                "fixed on save.")
            note.setStyleSheet("color: gray")
            v.addWidget(note)

        def _reload15(self):
            from .edc15.vss15 import (speed_settings as _edc15_speed,
                                      FGG_SOURCES as _FGG_SRC)
            st = _edc15_speed(self.session)
            blocks = st.blocks
            self.tbl.setRowCount(len(blocks))
            for i, b in enumerate(blocks):
                src = st.source.get(b.ds.label)
                srctxt = _FGG_SRC.get(src, "?" if src is None else str(src))
                if b.name.startswith("fgwDA"):
                    sf = str(b.values.get("SF", ""))
                    tmx = str(b.values.get("TMX", ""))
                    ppr = "%.2f" % b.imp_per_rev
                else:
                    sf = tmx = ppr = "—"
                    srctxt = "(tacho self-learn)"
                act = "●" if b.active else ("" if b.active is None else "")
                cells = [b.ds.label, b.name, srctxt, act, sf, tmx, ppr]
                for j, txt in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(txt)
                    if j in (4, 5, 6):
                        it.setFont(QtGui.QFont("Consolas"))
                    self.tbl.setItem(i, j, it)
                if self._has_cmp:
                    cb = (self._cmp15.blocks[i]
                          if i < len(self._cmp15.blocks) else None)
                    if cb is not None and cb.name.startswith("fgwDA"):
                        ccells = [str(cb.values.get("SF", "")),
                                  str(cb.values.get("TMX", "")),
                                  "%.2f" % cb.imp_per_rev]
                    else:
                        ccells = ["\u2014", "\u2014", "\u2014"]
                    for j, txt in enumerate(ccells):
                        it = QtWidgets.QTableWidgetItem(txt)
                        it.setFont(QtGui.QFont("Consolas"))
                        self.tbl.setItem(i, 7 + j, it)
            self._ds_changed15()
            self._recalc15()

        def _ds_changed15(self, *a):
            lbl = self.ds_box.currentData()
            e = self._cowvar.get(lbl)
            if e is None:
                return
            cur = self.session.binfile.data[e.addr]
            idx = self.src15_box.findData(cur)
            if idx >= 0:
                self.src15_box.setCurrentIndex(idx)

        def _corr_mode_changed(self, *a):
            speed = self.corr_mode.currentData() == "speed"
            self.tyre_w.setVisible(not speed)
            self.speed_w.setVisible(speed)
            self._recalc15()

        def _ratio15(self):
            from .edc15.vss15 import (tyre_circumference_mm as _tyre_circ,
                                      speed_error_ratio as _ser)
            try:
                if self.corr_mode.currentData() == "speed":
                    return _ser(float(self.spd_actual.text()),
                                float(self.spd_reported.text()))
                old = _tyre_circ(self.tyre_old.text())
                new = _tyre_circ(self.tyre_new.text())
                return new / old if old else None
            except Exception:
                return None

        def _recalc15(self, *a):
            from .edc15.vss15 import speed_settings as _edc15_speed
            speed = self.corr_mode.currentData() == "speed"
            ratio = self._ratio15()
            if ratio is None:
                self.result15.setText(
                    "<span style='color:#c0392b'>%s</span>"
                    % ("Enter the indicated and actual speed (same units, both "
                       "&gt; 0)." if speed else "Enter tyre sizes like "
                       "195/65R15."))
                self.apply15.setEnabled(False)
                return
            st = _edc15_speed(self.session)
            ex = ""
            for b in st.blocks:
                if b.name.startswith("fgwDA") and b.valid:
                    ex = ("  e.g. SF %d&rarr;%d, TMX %d&rarr;%d"
                          % (b.values["SF"], round(b.values["SF"] * ratio),
                             b.values["TMX"], round(b.values["TMX"] * ratio)))
                    break
            label = "Correction" if speed else "Circumference"
            self.result15.setText("%s ratio <b>%.5f</b> (%+.2f%%).%s"
                                  % (label, ratio, (ratio - 1) * 100, ex))
            self.apply15.setEnabled(abs(ratio - 1.0) > 1e-6)

        def _apply_rescale15(self):
            from .edc15.vss15 import rescale as _edc15_rescale
            ratio = self._ratio15()
            if ratio is None:
                return
            cmd = _edc15_rescale(self.session, ratio, self.ktg_chk.isChecked())
            if cmd is None:
                QtWidgets.QMessageBox.information(
                    self, "Speed correction", "Nothing to change at this ratio.")
                return
            self._reload()
            self._after_write()

        def _apply_source15(self):
            lbl = self.ds_box.currentData()
            e = self._cowvar.get(lbl)
            if e is None:
                return
            want = int(self.src15_box.currentData())
            if self.session.binfile.data[e.addr] != want:
                self.session.edit_bytes(e.addr, bytes([want]))
                self._reload()
                self._after_write()

    # the 3-D surface (Ctrl+3) lives in gui_surface3d; the names stay here
    from .gui_surface3d import Surface3DView, SurfacePanel  # noqa: E402,F401


    class SweepDialog(QtWidgets.QDialog):
        """Needle / LED sweep (and DSG-needle) patches for a VDO cluster.

        Lists the bundled patches -- the ones for THIS cluster's exact ROM
        build first, the rest greyed with the reason -- each with its author's
        credit, licence and source; loads a KWHack ``.patch`` file or a raw
        ``.bin`` block from disk; and applies the chosen one to the open
        EEPROM as one undoable edit. Writing it to the car is the cluster
        Write in the read/write dialog (only the changed blocks are sent)."""

        def __init__(self, session: TunerSession, parent=None):
            super().__init__(parent)
            from .vdo import sweep as _sw
            self._sw = _sw
            self.session = session
            self.setWindowTitle("Cluster needle sweep")
            self.setWindowIcon(app_icon())
            self.resize(760, 560)
            try:
                self._library = _sw.load_library()
            except (OSError, ValueError) as ex:
                self._library = []
                QtWidgets.QMessageBox.warning(
                    self, "Needle sweep",
                    "The bundled patch library could not be read:\n%s" % ex)
            self._loaded = []                   # patches loaded from files
            v = QtWidgets.QVBoxLayout(self)

            intro = QtWidgets.QLabel(
                "A sweep patch is a small program the cluster loads from its "
                "EEPROM at power-up. Each one is written for <b>one exact ROM "
                "build</b> and replaces the patch block already there, so only "
                "patches for this cluster's build can be applied.")
            intro.setWordWrap(True)
            intro.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                QtWidgets.QSizePolicy.Policy.Minimum)
            v.addWidget(intro)

            box = QtWidgets.QGroupBox("This cluster")
            form = QtWidgets.QFormLayout(box)
            self.rom_lbl = QtWidgets.QLabel()
            self.rom_lbl.setWordWrap(True)
            form.addRow("ROM build:", self.rom_lbl)
            self.rom_box = QtWidgets.QComboBox()
            self.rom_box.setToolTip(
                "The file does not say which ROM build the cluster runs (it "
                "was not read from the car and has no patch header). Read the "
                "cluster with the read/write dialog, or pick the build printed "
                "on the cluster's label to confirm it.")
            self.rom_box.currentIndexChanged.connect(self._fill)
            self.rom_row_lbl = QtWidgets.QLabel("Confirm build:")
            form.addRow(self.rom_row_lbl, self.rom_box)
            self.cur_lbl = QtWidgets.QLabel()
            self.cur_lbl.setWordWrap(True)
            form.addRow("Patch block now:", self.cur_lbl)
            v.addWidget(box)

            split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
            self.tree = QtWidgets.QTreeWidget()
            self.tree.setRootIsDecorated(False)
            self.tree.setAlternatingRowColors(True)
            self.tree.setHeaderLabels(["Patch", "ROM build", "Author",
                                       "Licence", "Size"])
            self.tree.header().setStretchLastSection(False)
            self.tree.header().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            for c in range(1, 5):
                self.tree.header().setSectionResizeMode(
                    c, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            self.tree.currentItemChanged.connect(self._selected)
            self.tree.itemDoubleClicked.connect(lambda *_a: self._apply())
            split.addWidget(self.tree)
            self.details = QtWidgets.QTextBrowser()
            self.details.setOpenExternalLinks(True)
            split.addWidget(self.details)
            split.setSizes([300, 180])
            v.addWidget(split, 1)

            row = QtWidgets.QHBoxLayout()
            self.all_chk = QtWidgets.QCheckBox("Show patches for other ROM builds")
            self.all_chk.toggled.connect(self._fill)
            row.addWidget(self.all_chk)
            row.addStretch(1)
            load = QtWidgets.QPushButton("Load patch file...")
            load.setToolTip("A KWHack .patch file, or a raw .bin patch block "
                            "(as kw1281test LoadEeprom takes)")
            load.clicked.connect(self._load_file)
            row.addWidget(load)
            self.lic_btn = QtWidgets.QPushButton("Licence...")
            self.lic_btn.clicked.connect(self._show_licence)
            row.addWidget(self.lic_btn)
            self.apply_btn = QtWidgets.QPushButton("Apply")
            self.apply_btn.setDefault(True)
            self.apply_btn.clicked.connect(self._apply)
            row.addWidget(self.apply_btn)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.close)
            row.addWidget(close)
            v.addLayout(row)
            self.refresh_from_session()

        # -- state ---------------------------------------------------------
        def _layout_roms(self):
            lay = self.session.vdo_definition.layout
            fams = set(self._sw._families_of_layout(lay))
            return sorted({p.rom_version for p in self._library + self._loaded
                           if p.rom_version[:6] in fams})

        def _assume(self):
            if self._sw.cluster_rom(self.session):
                return None
            return self.rom_box.currentData()

        def refresh_from_session(self):
            rom = self._sw.cluster_rom(self.session)
            ident = self.session.vdo_identity
            if rom:
                src = {"cluster": "reported by the cluster when it was read",
                       "patch header": "from the patch block's header"}.get(
                    getattr(ident, "rom_source", ""), "")
                self.rom_lbl.setText("<b>%s</b>%s" % (
                    rom, "  (%s)" % src if src else ""))
            else:
                self.rom_lbl.setText(
                    "<b>not identified</b> -- read the cluster, or confirm the "
                    "build from its label below")
            want = None if rom else self.rom_box.currentData()
            self.rom_box.blockSignals(True)
            self.rom_box.clear()
            self.rom_box.addItem("(not confirmed)", None)
            for r in self._layout_roms():
                self.rom_box.addItem(r, r)
            i = self.rom_box.findData(want) if want else 0
            self.rom_box.setCurrentIndex(max(0, i))
            self.rom_box.blockSignals(False)
            self.rom_box.setVisible(not rom)
            self.rom_row_lbl.setVisible(not rom)
            self.cur_lbl.setText(self._sw.current_patch(
                self.session, self._library + self._loaded))
            self._fill()

        def _fill(self, *_a):
            keep = self._current()
            assume = self._assume()
            self.tree.clear()
            fit, rest = [], []
            for p in self._loaded + self._library:
                ok, why = self._sw.check(self.session, p, assume)
                (fit if ok else rest).append((p, ok, why))
            shown = fit + (rest if self.all_chk.isChecked() else [])
            sel = None
            for p, ok, why in shown:
                it = QtWidgets.QTreeWidgetItem([
                    p.name, p.rom_version,
                    p.author or ("file" if p.source != "library" else ""),
                    p.license or "not stated", "%d bytes" % p.size])
                it.setData(0, QtCore.Qt.ItemDataRole.UserRole, (p, ok, why))
                if not ok:
                    for c in range(5):
                        it.setForeground(c, self.palette().brush(
                            QtGui.QPalette.ColorGroup.Disabled,
                            QtGui.QPalette.ColorRole.Text))
                    it.setToolTip(0, "\n".join(why))
                self.tree.addTopLevelItem(it)
                if p is keep or (sel is None and ok):
                    sel = it
            if sel is None and self.tree.topLevelItemCount():
                sel = self.tree.topLevelItem(0)
            self.tree.setCurrentItem(sel)
            if not shown:
                n = len(self._library)
                self.details.setHtml(
                    "<p>None of the %d bundled patches is for this cluster's "
                    "ROM build.</p><p>Tick <i>Show patches for other ROM "
                    "builds</i> to see what exists, or <i>Load patch "
                    "file...</i> if you have one for this build.</p>" % n)
            self._selected()

        def _current(self):
            it = self.tree.currentItem()
            d = it.data(0, QtCore.Qt.ItemDataRole.UserRole) if it else None
            return d[0] if d else None

        def _selected(self, *_a):
            import html as _h
            it = self.tree.currentItem()
            d = it.data(0, QtCore.Qt.ItemDataRole.UserRole) if it else None
            self.apply_btn.setEnabled(bool(d and d[1]))
            self.lic_btn.setEnabled(bool(d and d[0].license_text()))
            if not d:
                return
            p, ok, why = d
            parts = ["<p><b>%s</b> &mdash; %s, %d bytes at 0x%03X</p>" % (
                _h.escape(p.name), _h.escape(p.rom_version), p.size, p.address)]
            for line in p.attribution.split("\n"):
                esc = _h.escape(line)
                if line.startswith("Source: http"):
                    url = _h.escape(line[8:])
                    esc = 'Source: <a href="%s">%s</a>' % (url, url)
                parts.append("<p>%s</p>" % esc)
            if not ok:
                parts.append("<p><b>Cannot be applied here:</b></p><ul>%s</ul>"
                             % "".join("<li>%s</li>" % _h.escape(w) for w in why))
            else:
                rep = self._sw.replaced_patch(self.session, p)
                if rep and rep[1] and rep[0] < rep[1]:
                    parts.append(
                        "<p><b>Note:</b> it replaces the patch block this "
                        "cluster carries now, and carries only %d of that "
                        "block's %d fix routines. On a stock cluster that "
                        "block is VDO's factory bug-fix patch.</p>" % rep)
                elif rep and rep[1]:
                    parts.append(
                        "<p>It replaces the patch block this cluster carries "
                        "now and carries all %d of its fix routines.</p>"
                        % rep[1])
            self.details.setHtml("".join(parts))

        # -- actions -------------------------------------------------------
        def _load_file(self):
            import os as _os
            from .recent import get_last_dir, set_last_dir
            path, _f = QtWidgets.QFileDialog.getOpenFileName(
                self, "Load sweep patch", get_last_dir("sweep"),
                "Sweep patches (*.patch *.bin);;All files (*)")
            if not path:
                return
            set_last_dir(path, "sweep")
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                p = self._parse_file(path, raw)
            except (OSError, ValueError) as ex:
                QtWidgets.QMessageBox.warning(
                    self, "Load sweep patch",
                    "Could not use %s:\n%s" % (_os.path.basename(path), ex))
                return
            if p is None:
                return
            self._loaded = [q for q in self._loaded if q.data != p.data] + [p]
            self.refresh_from_session()
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                if it.data(0, QtCore.Qt.ItemDataRole.UserRole)[0] is p:
                    self.tree.setCurrentItem(it)
                    break
            else:
                self.all_chk.setChecked(True)

        def _parse_file(self, path, raw):
            import os as _os
            sw = self._sw
            name = _os.path.basename(path)
            if b"Data:" in raw[:4096]:
                return sw.parse_patch_file(raw.decode("utf-8", "replace"),
                                           source=name)
            # A raw block: its header names the ROM build; the load address
            # is this cluster's patch-module address.
            roms = sw.rom_versions_matching(raw)
            if not roms:
                raise ValueError(
                    "this is neither a .patch file nor a patch block whose "
                    "header names a known ROM build")
            rom = roms[0]
            if len(roms) > 1:
                rom, ok = QtWidgets.QInputDialog.getItem(
                    self, "Load sweep patch",
                    "The block's header fits more than one ROM build. Which "
                    "is it for?", roms, 0, False)
                if not ok:
                    return None
            pm = self.session.vdo_definition.patch_module
            if not pm:
                raise ValueError("no patch-module address is known for this "
                                 "cluster family")
            return sw.from_block(raw, pm["address"], rom,
                                 name=_os.path.splitext(name)[0], source=name)

        def _show_licence(self):
            p = self._current()
            if p is None:
                return
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Licence — %s" % p.name)
            dlg.resize(640, 560)
            lay = QtWidgets.QVBoxLayout(dlg)
            txt = QtWidgets.QPlainTextEdit(p.attribution + "\n\n" + p.license_text())
            txt.setReadOnly(True)
            lay.addWidget(txt)
            bb = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject)
            lay.addWidget(bb)
            dlg.exec()

        def _apply(self):
            it = self.tree.currentItem()
            d = it.data(0, QtCore.Qt.ItemDataRole.UserRole) if it else None
            if not d or not d[1]:
                return
            p = d[0]
            assume = self._assume()
            rep = self._sw.replaced_patch(self.session, p)
            msg = ["Apply %s to this cluster file?" % p.label, ""]
            if assume:
                msg.append("You confirmed the cluster runs %s. A patch for the "
                           "wrong build can stop the cluster booting until its "
                           "EEPROM is restored with a programmer." % assume)
                msg.append("")
            if rep and rep[1] and rep[0] < rep[1]:
                msg.append("It replaces the patch block the cluster carries now "
                           "and carries only %d of its %d fix routines (on a "
                           "stock cluster that block is VDO's factory bug-fix "
                           "patch)." % rep)
                msg.append("")
            msg.append("The change can be undone (Ctrl+Z). Nothing is sent to "
                       "the car until you write the cluster; the read/write "
                       "dialog keeps a backup of what it replaces.")
            yes = QtWidgets.QMessageBox.StandardButton.Yes
            if QtWidgets.QMessageBox.question(
                    self, "Apply sweep patch", "\n".join(msg),
                    yes | QtWidgets.QMessageBox.StandardButton.Cancel) != yes:
                return
            try:
                done = self._sw.apply(self.session, p, assume)
            except ValueError as ex:
                QtWidgets.QMessageBox.warning(self, "Apply sweep patch", str(ex))
                return
            par = self.parent()
            if par is not None:
                try:
                    par.model.refresh()
                    par._status()
                except (AttributeError, RuntimeError):
                    pass
            self.refresh_from_session()
            QtWidgets.QMessageBox.information(self, "Apply sweep patch", done)

    class DiffListDialog(QtWidgets.QDialog):
        """Lists every parameter that differs between the primary and the
        comparison, with how many cells changed and the first change, and jumps
        to a parameter in the editor on double-click (like 6HP's difference
        report). Values are compared in physical units through the KB identity,
        so it works across calibrations with different internal layouts."""

        def __init__(self, primary, comparison, rows, parent=None):
            super().__init__(parent)
            self.primary = primary
            self.comparison = comparison
            self._rows = rows
            self.setWindowTitle("Differences — %s vs %s"
                                % (primary.binfile.name,
                                   comparison.binfile.name))
            self.setWindowIcon(app_icon())
            self.resize(760, 560)
            v = QtWidgets.QVBoxLayout(self)
            self.summary_lbl = QtWidgets.QLabel()
            v.addWidget(self.summary_lbl)

            blk_row = QtWidgets.QHBoxLayout()
            blk_row.addWidget(QtWidgets.QLabel("Show:"))
            self.blk_prog = QtWidgets.QCheckBox("Program/boot")
            self.blk_cal = QtWidgets.QCheckBox("Calibration")
            self.blk_cal.setChecked(True)
            self.blk_mpc = QtWidgets.QCheckBox("MPC / internal flash")
            self.blk_eeprom = QtWidgets.QCheckBox("EEPROM")
            for chk in (self.blk_prog, self.blk_cal, self.blk_mpc, self.blk_eeprom):
                chk.stateChanged.connect(self._rebuild)
                blk_row.addWidget(chk)
            blk_row.addStretch(1)
            v.addLayout(blk_row)

            self.excl_checksum_chk = QtWidgets.QCheckBox(
                "Exclude checksum/digest/signature bytes -- they recompute "
                "on their own whenever anything else in their block "
                "changes, so a difference there alone isn't meaningful")
            self.excl_checksum_chk.setChecked(True)
            self.excl_checksum_chk.stateChanged.connect(self._rebuild)
            v.addWidget(self.excl_checksum_chk)

            note = QtWidgets.QLabel(
                "Program/boot, MPC and EEPROM rows are raw byte ranges, "
                "address-literal, only meaningful between binaries with "
                "the same layout; MPC/EEPROM need a container that "
                "carries them (.dim / .sgo / .edc16).")
            note.setWordWrap(True)
            f = note.font()
            f.setItalic(True)
            note.setFont(f)
            v.addWidget(note)

            self.tbl = QtWidgets.QTableWidget(0, 4)
            self.tbl.setHorizontalHeaderLabels(
                ["Parameter", "Group", "Cells changed", "First change"])
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.tbl.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows)
            self.tbl.horizontalHeader().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            self.tbl.cellDoubleClicked.connect(self._open)
            v.addWidget(self.tbl)

            btns = QtWidgets.QHBoxLayout()
            exp = QtWidgets.QPushButton("Copy list")
            exp.clicked.connect(self._copy)
            btns.addWidget(exp)
            btns.addStretch(1)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.accept)
            btns.addWidget(close)
            v.addLayout(btns)

            self._rebuild()

        def _rebuild(self):
            from . import patch as _patch
            rows = self._rows if self.blk_cal.isChecked() else []
            wanted = []
            if self.blk_prog.isChecked():
                wanted.append(_patch.PROGRAM_BLOCK)
            if self.blk_mpc.isChecked():
                wanted.append(_patch.MPC_BLOCK)
            if self.blk_eeprom.isChecked():
                wanted.append(_patch.EEPROM_BLOCK)
            raw_rows = (_patch.diff_outside_calibration(
                            self.primary, self.comparison, blocks=wanted,
                            exclude_checksums=self.excl_checksum_chk.isChecked())
                       if wanted else [])
            self.summary_lbl.setText(
                "<b>%d parameter(s) differ%s.</b> Double-click a parameter "
                "row to open it in the editor behind this window "
                "(differing cells are highlighted there)."
                % (len(rows),
                   ", %d raw byte range(s) in the selected block(s)"
                   % len(raw_rows) if raw_rows else ""))

            self._ents = []
            self.tbl.setRowCount(len(rows) + len(raw_rows))
            for i, (e, cp) in enumerate(rows):
                self._ents.append(e)
                nchg, first = self._summarize(e, cp)
                cells = [_english(e), tier_name(e.tier),
                         "structure differs" if nchg is None else str(nchg),
                         first]
                for j, txt in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(txt)
                    if j == 2:
                        it.setTextAlignment(int(QtCore.Qt.AlignRight
                                                | QtCore.Qt.AlignVCenter))
                    self.tbl.setItem(i, j, it)
            a = bytes(self.primary.binfile.data)
            b = bytes(self.comparison.binfile.data)
            for k, (label, addr, length) in enumerate(raw_rows):
                i = len(rows) + k
                self._ents.append(None)
                first = "@0x%X: %s → %s" % (
                    addr, a[addr:addr + 1].hex() if addr < len(a) else "??",
                    b[addr:addr + 1].hex() if addr < len(b) else "??")
                cells = ["%s @ 0x%X" % (label, addr), "raw bytes",
                         str(length), first]
                for j, txt in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(txt)
                    if j == 2:
                        it.setTextAlignment(int(QtCore.Qt.AlignRight
                                                | QtCore.Qt.AlignVCenter))
                    self.tbl.setItem(i, j, it)
            self.tbl.resizeColumnsToContents()
            self.tbl.horizontalHeader().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeMode.Stretch)

        def _summarize(self, e, cp):
            """(#changed cells, 'first change' text) or (None, ...) when the
            two sides have different dimensions."""
            if e.kind == "constant":
                a, b = e.cell(0, 0), cp.cell(0, 0)
                return (1 if a != b else 0), "%s → %s" % (_fmt(a), _fmt(b))
            pairs = _cell_pairs(e, cp)
            if pairs is None:
                return None, "%s vs %s" % (_shape_text(e), _shape_text(cp))
            run = _is_run(e)            # a 1-D table: its place along the run
            n = 0
            first = ""
            for (r, c), (r2, c2) in pairs:
                a, b = e.cell(r, c), cp.cell(r2, c2)
                if a != b:
                    n += 1
                    if not first:
                        where = ("[%d]" % (r * e.cols + c) if run
                                 else "[%d,%d]" % (r, c))
                        first = "%s %s → %s" % (where, _fmt(a), _fmt(b))
            axes = _axis_changes(e, cp)
            if axes:
                ax, i, a, b = axes[0]
                note = "%s axis[%d] %s → %s" % (ax.upper(), i, _fmt(a), _fmt(b))
                if len(axes) > 1:
                    note += " (+%d more axis point(s))" % (len(axes) - 1)
                first = "%s; %s" % (first, note) if first else note
            return n, first

        def _open(self, row, _col):
            if (0 <= row < len(self._ents) and self._ents[row] is not None
                   and self.parent() is not None):
                self.parent().show_entity(self._ents[row])
                self.parent().raise_()
                self.parent().activateWindow()

        def _copy(self):
            lines = ["Parameter\tGroup\tCells changed\tFirst change"]
            for i in range(self.tbl.rowCount()):
                lines.append("\t".join(
                    self.tbl.item(i, j).text() for j in range(4)))
            QtWidgets.QApplication.clipboard().setText("\n".join(lines))


    class PatchBuilderDialog(QtWidgets.QDialog):
        """Pick which changed parameters -- and which undefined-data byte
        ranges -- go into a saved patch file, from the primary/comparison
        pair already loaded for live comparison.

        Diffed by KB identity (``ovtsuite.patch.diff_entities``), so this
        works across two binaries with different internal layouts, exactly
        like ``DiffListDialog`` above; unlike that dialog, selections here
        become a portable file rather than just a report.
        """

        def __init__(self, primary, comparison, parent=None, on_swap=None):
            """The patch turns ``primary`` into ``comparison`` (it carries
            the comparison's values). ``on_swap``: show **Swap direction**,
            which closes this and calls it."""
            super().__init__(parent)
            import html
            from . import patch as _patch
            self._patch = _patch
            self.primary = primary
            self.comparison = comparison
            self.setWindowTitle("Generate Patch — %s to %s"
                                % (primary.binfile.name, comparison.binfile.name))
            self.setWindowIcon(app_icon())
            self.resize(860, 640)

            self._entries = _patch.diff_entities(primary, comparison)
            self._gaps = _patch.diff_gaps(primary, comparison)

            v = QtWidgets.QVBoxLayout(self)
            way = QtWidgets.QHBoxLayout()
            self.direction = QtWidgets.QLabel(
                "From <b>%s</b> to <b>%s</b>: applying the patch makes a "
                "binary like the first one into the second."
                % (html.escape(primary.binfile.name),
                   html.escape(comparison.binfile.name)))
            self.direction.setWordWrap(True)
            way.addWidget(self.direction, 1)
            if on_swap is not None:
                swap = QtWidgets.QPushButton("Swap direction")
                swap.setToolTip("Make the patch the other way round")

                def _swap():
                    self.reject()
                    QtCore.QTimer.singleShot(0, on_swap)
                swap.clicked.connect(_swap)
                way.addWidget(swap)
            v.addLayout(way)
            v.addWidget(QtWidgets.QLabel(
                "<b>%d parameter(s) differ.</b> A patch is applied to a "
                "target by parameter identity, not by address, so it can "
                "target a binary with a different internal layout -- check "
                "the ones to carry." % len(self._entries)))

            top = QtWidgets.QHBoxLayout()
            all_btn = QtWidgets.QPushButton("Check all")
            all_btn.clicked.connect(lambda: self._set_all(self.tbl, True))
            none_btn = QtWidgets.QPushButton("Check none")
            none_btn.clicked.connect(lambda: self._set_all(self.tbl, False))
            top.addWidget(all_btn)
            top.addWidget(none_btn)
            top.addStretch(1)
            v.addLayout(top)

            self.tbl = QtWidgets.QTableWidget(0, 5)
            self.tbl.setHorizontalHeaderLabels(
                ["", "Parameter", "Cells changed", "First change", "Grade"])
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.tbl.setRowCount(len(self._entries))
            for i, e in enumerate(self._entries):
                self._check_item(self.tbl, i, 0, e.selected)
                ch = ("structure differs" if e.changed_cells is None
                     else "%d/%d" % (e.changed_cells, e.total_cells))
                if getattr(e, "changed_axes", 0):
                    ch += " + %d axis" % e.changed_axes
                for j, txt in enumerate(
                       (e.title(), ch, e.first_change, e.source_grade), start=1):
                    self.tbl.setItem(i, j, QtWidgets.QTableWidgetItem(txt))
            self.tbl.resizeColumnsToContents()
            self.tbl.horizontalHeader().setSectionResizeMode(
                1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            v.addWidget(self.tbl, 2)

            v.addWidget(QtWidgets.QLabel(
                "<b>%d undefined-data range(s) also differ</b> (no "
                "identified parameter covers them). Unchecked by default -- "
                "these are address-literal and only meaningful for a target "
                "of the same family/sub-family." % len(self._gaps)))
            gtop = QtWidgets.QHBoxLayout()
            gall = QtWidgets.QPushButton("Check all")
            gall.clicked.connect(lambda: self._set_all(self.gtbl, True))
            gnone = QtWidgets.QPushButton("Check none")
            gnone.clicked.connect(lambda: self._set_all(self.gtbl, False))
            gtop.addWidget(gall)
            gtop.addWidget(gnone)
            gtop.addStretch(1)
            v.addLayout(gtop)

            self.gtbl = QtWidgets.QTableWidget(0, 3)
            self.gtbl.setHorizontalHeaderLabels(["", "Address", "Length"])
            self.gtbl.verticalHeader().setVisible(False)
            self.gtbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.gtbl.setRowCount(len(self._gaps))
            for i, (s, e) in enumerate(self._gaps):
                self._check_item(self.gtbl, i, 0, False)
                self.gtbl.setItem(i, 1, QtWidgets.QTableWidgetItem("0x%X" % s))
                self.gtbl.setItem(i, 2, QtWidgets.QTableWidgetItem(
                    "%d bytes" % (e - s)))
            self.gtbl.resizeColumnsToContents()
            v.addWidget(self.gtbl, 1)

            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            save = QtWidgets.QPushButton("Save patch...")
            save.clicked.connect(self._save)
            btns.addWidget(save)
            cancel = QtWidgets.QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btns.addWidget(cancel)
            v.addLayout(btns)

        @staticmethod
        def _check_item(tbl, row, col, checked):
            it = QtWidgets.QTableWidgetItem()
            it.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable
                       | QtCore.Qt.ItemFlag.ItemIsEnabled)
            it.setCheckState(QtCore.Qt.CheckState.Checked if checked
                             else QtCore.Qt.CheckState.Unchecked)
            tbl.setItem(row, col, it)

        @staticmethod
        def _set_all(tbl, checked):
            state = (QtCore.Qt.CheckState.Checked if checked
                    else QtCore.Qt.CheckState.Unchecked)
            for r in range(tbl.rowCount()):
                tbl.item(r, 0).setCheckState(state)

        def _save(self):
            import os as _os
            for i, e in enumerate(self._entries):
                e.selected = (self.tbl.item(i, 0).checkState()
                             == QtCore.Qt.CheckState.Checked)
            blocks = []
            for i, (s, e) in enumerate(self._gaps):
                if self.gtbl.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked:
                    blocks.append(self._patch.build_block_entry(
                        self.primary, self.comparison, s, e))
            selected = [e for e in self._entries if e.selected]
            if not selected and not blocks:
                QtWidgets.QMessageBox.information(
                    self, "Generate Patch", "Nothing is checked.")
                return
            default = "%s_to_%s.patch" % (
                _os.path.splitext(self.primary.binfile.name)[0],
                _os.path.splitext(self.comparison.binfile.name)[0])
            pt = self._patch.new_patch(self.primary, self.comparison,
                                       selected, blocks)
            ws = self.parent()
            ref = getattr(ws, "project_ref", None)
            if ref is not None:
                # kept in the project by default; Project panel > Export
                # writes it to a file whenever wanted
                SB = QtWidgets.QMessageBox.StandardButton
                box = QtWidgets.QMessageBox(
                    QtWidgets.QMessageBox.Icon.Question, "Generate Patch",
                    "Keep the patch (%d parameter(s), %d block(s)) in the "
                    "project %s?" % (len(selected), len(blocks), ref.doc.title),
                    SB.Cancel, self)
                keep = box.addButton("Keep in the project",
                                     QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                tofile = box.addButton("Save as a file...",
                                       QtWidgets.QMessageBox.ButtonRole.ActionRole)
                box.setDefaultButton(keep)
                box.exec()
                if box.clickedButton() is keep:
                    _gp.keep_patch(ws, pt, default)
                    self.accept()
                    return
                if box.clickedButton() is not tofile:
                    return
            from .recent import get_last_dir, set_last_dir
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save patch", _os.path.join(get_last_dir("patch"), default),
                "Patch files (*.patch)")
            if not path:
                return
            set_last_dir(path, "patch")
            pt.save(path)
            QtWidgets.QMessageBox.information(
                self, "Generate Patch",
                "Saved %d parameter(s) and %d block(s) to %s"
                % (len(selected), len(blocks), _os.path.basename(path)))
            self.accept()


    class PatchApplyDialog(QtWidgets.QDialog):
        """Apply a saved patch to the CURRENTLY LOADED binary, resolving
        every entry by name against ITS OWN identified entities -- never the
        patch's captured addresses (``ovtsuite.patch.apply_patch``).

        A target parameter whose own placement is not established
        (bracketed/projected -- ``model.TRUSTED_PLACEMENT``) needs an
        explicit per-row choice, Discard or Apply anyway; a trusted one needs
        no confirmation and has no choice to make. An undefined-data block
        captured from a different family than this target gets the same
        treatment.
        """

        def __init__(self, target, pt, title, parent=None):
            super().__init__(parent)
            from . import patch as _patch
            from .model import placement_trusted
            self._patch = _patch
            self._trusted = placement_trusted
            self.target = target
            self.pt = pt
            self._title = title
            self.setWindowTitle("Apply Patch — %s" % title)
            self.setWindowIcon(app_icon())
            self.resize(880, 600)

            v = QtWidgets.QVBoxLayout(self)
            v.addWidget(QtWidgets.QLabel(
                "<b>%s -> %s</b>  (captured %s -> %s)"
                % (pt.source_name, pt.comparison_name, pt.source_family,
                   pt.comparison_family)))

            self.tbl = QtWidgets.QTableWidget(0, 5)
            self.tbl.setHorizontalHeaderLabels(
                ["", "Parameter", "Target status", "Captured grade",
                 "If unestablished"])
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            entries = [e for e in pt.entries]
            self.tbl.setRowCount(len(entries) + len(pt.blocks))
            self._combos = {}
            row = 0
            for e in entries:
                self._check_item(row, e.selected)
                self.tbl.setItem(row, 1, QtWidgets.QTableWidgetItem(e.title()))
                te = _patch.find_entity(target, e.name)
                if te is None:
                    status = "not found in this binary"
                    combo = None
                elif self._trusted(te):
                    status = "%s (established)" % te.source
                    combo = None
                else:
                    status = "%s (NOT established)" % te.source
                    combo = QtWidgets.QComboBox()
                    combo.addItems(["Discard", "Apply anyway"])
                self.tbl.setItem(row, 2, QtWidgets.QTableWidgetItem(status))
                self.tbl.setItem(row, 3, QtWidgets.QTableWidgetItem(e.source_grade))
                if combo is not None:
                    self.tbl.setCellWidget(row, 4, combo)
                    self._combos[("entry", e.name)] = combo
                row += 1
            for b in pt.blocks:
                key = "block@0x%X" % b.addr
                self._check_item(row, b.selected)
                self.tbl.setItem(row, 1, QtWidgets.QTableWidgetItem(
                    "%s (%d bytes)" % (key, b.length)))
                cross = (getattr(target, "family", "") != b.source_family)
                if cross:
                    status = "captured from %r, target is %r" % (
                        b.source_family, getattr(target, "family", ""))
                    combo = QtWidgets.QComboBox()
                    combo.addItems(["Discard", "Apply anyway"])
                    self.tbl.setCellWidget(row, 4, combo)
                    self._combos[("block", key)] = combo
                else:
                    status = "same family"
                self.tbl.setItem(row, 2, QtWidgets.QTableWidgetItem(status))
                self.tbl.setItem(row, 3, QtWidgets.QTableWidgetItem(""))
                row += 1
            self.tbl.resizeColumnsToContents()
            self.tbl.horizontalHeader().setSectionResizeMode(
                1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            v.addWidget(self.tbl, 1)

            trust_all = QtWidgets.QCheckBox(
                "Apply every unestablished/cross-family entry anyway")
            trust_all.toggled.connect(self._toggle_trust_all)
            v.addWidget(trust_all)

            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            apply_btn = QtWidgets.QPushButton("Apply")
            apply_btn.clicked.connect(self._apply)
            btns.addWidget(apply_btn)
            cancel = QtWidgets.QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btns.addWidget(cancel)
            v.addLayout(btns)

        def _check_item(self, row, checked):
            it = QtWidgets.QTableWidgetItem()
            it.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable
                       | QtCore.Qt.ItemFlag.ItemIsEnabled)
            it.setCheckState(QtCore.Qt.CheckState.Checked if checked
                             else QtCore.Qt.CheckState.Unchecked)
            self.tbl.setItem(row, 0, it)

        def _toggle_trust_all(self, checked):
            for combo in self._combos.values():
                combo.setCurrentIndex(1 if checked else 0)

        def _apply(self):
            row = 0
            for e in self.pt.entries:
                e.selected = (self.tbl.item(row, 0).checkState()
                             == QtCore.Qt.CheckState.Checked)
                row += 1
            for b in self.pt.blocks:
                b.selected = (self.tbl.item(row, 0).checkState()
                             == QtCore.Qt.CheckState.Checked)
                row += 1
            dispositions = {}
            for (_kind, key), combo in self._combos.items():
                if combo.currentIndex() == 1:
                    dispositions[key] = self._patch.APPLY
            res = self._patch.apply_patch(
                self.pt, self.target, dispositions=dispositions,
                default_disposition=self._patch.DISCARD,
                source_label=self._title, label="apply patch")
            lines = ["Applied %d, skipped %d (%d conflicted with edits already "
                    "made this session)."
                    % (len(res.applied), len(res.skipped), res.conflicts)]
            if res.skipped:
                lines.append("")
                lines.append("Skipped:")
                lines.extend("  %s: %s" % (n, r) for n, r in res.skipped[:40])
                if len(res.skipped) > 40:
                    lines.append("  ...and %d more" % (len(res.skipped) - 40))
            if res.warnings:
                lines.append("")
                lines.append("Warnings:")
                lines.extend("  %s" % w for w in res.warnings[:20])
            if res.record_id:
                lines.append("")
                lines.append(
                    "Recorded as patch %s. Save to make it removable later "
                    "with Edit > Remove patch..." % res.record_id)
            QtWidgets.QMessageBox.information(self, "Apply Patch", "\n".join(lines))
            self.accept()


    class RemovePatchDialog(QtWidgets.QDialog):
        """Lists every patch apply (and revert of one) found for the
        currently loaded binary, from its change log alone -- no original
        patch file needed -- and reverts the one the user picks
        (``ovtsuite.patch.revert_applied_patch``).

        A revert restores the pre-patch value at every address the patch
        touched; it is itself logged, so it shows up here too and can be
        reverted right back if it turns out to be unwanted.
        """

        def __init__(self, target, parent=None):
            super().__init__(parent)
            from . import patch as _patch
            self._patch = _patch
            self.target = target
            self.setWindowTitle("Remove Patch — %s" % target.binfile.name)
            self.setWindowIcon(app_icon())
            self.resize(760, 440)

            v = QtWidgets.QVBoxLayout(self)
            self._records = _patch.list_applied_patches(target)
            if not self._records:
                v.addWidget(QtWidgets.QLabel(
                    "No patch-apply records found for this binary. A patch "
                    "apply is only recorded once this file has been saved "
                    "(or since the last apply, this session)."))
            else:
                v.addWidget(QtWidgets.QLabel(
                    "<b>%d record(s).</b> Reverting restores the pre-patch "
                    "value at every address the patch touched, using ONLY "
                    "this change log -- the original patch file is not "
                    "needed." % len(self._records)))

            self.tbl = QtWidgets.QTableWidget(0, 5)
            self.tbl.setHorizontalHeaderLabels(
                ["When", "Status", "Source", "Parameters", "Blocks"])
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.tbl.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows)
            self.tbl.setSelectionMode(
                QtWidgets.QAbstractItemView.SingleSelection)
            self.tbl.setRowCount(len(self._records))
            for i, r in enumerate(self._records):
                cells = [r.get("ts", ""),
                        "pending (unsaved)" if r.get("pending") else "saved",
                        r.get("source", ""), str(r.get("n_entries", 0)),
                        str(r.get("n_blocks", 0))]
                for j, txt in enumerate(cells):
                    self.tbl.setItem(i, j, QtWidgets.QTableWidgetItem(txt))
            self.tbl.resizeColumnsToContents()
            self.tbl.horizontalHeader().setSectionResizeMode(
                2, QtWidgets.QHeaderView.ResizeMode.Stretch)
            v.addWidget(self.tbl, 1)

            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            self.revert_btn = QtWidgets.QPushButton("Revert selected")
            self.revert_btn.setEnabled(False)
            self.revert_btn.clicked.connect(self._revert)
            btns.addWidget(self.revert_btn)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.reject)
            btns.addWidget(close)
            v.addLayout(btns)
            self.tbl.itemSelectionChanged.connect(
                lambda: self.revert_btn.setEnabled(bool(self.tbl.selectedItems())))

        def _revert(self):
            rows = self.tbl.selectionModel().selectedRows()
            if not rows:
                return
            record = self._records[rows[0].row()]
            if QtWidgets.QMessageBox.question(
                    self, "Revert Patch",
                    "Revert the patch from %r (%d parameter(s), %d block(s))?"
                    % (record.get("source", "?"), record.get("n_entries", 0),
                       record.get("n_blocks", 0))
                    ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            rv = self._patch.revert_applied_patch(self.target, record)
            lines = ["Reverted: %d byte range(s) restored." % rv.n_writes]
            if rv.warnings:
                lines.append("")
                lines.extend("  %s" % w for w in rv.warnings)
            QtWidgets.QMessageBox.information(self, "Revert Patch", "\n".join(lines))
            self.accept()


    class VersionHistoryDialog(QtWidgets.QDialog):
        """The loaded binary's save history: name checkpoints, merge adjacent
        ones, revert to one.

        Every save is a checkpoint, logged automatically in the ``.log``
        beside the binary (``ovtsuite.history``). A name is typed straight
        into the Name column. Merging rewrites only the history -- several
        adjacent saves become one checkpoint holding their net change -- and
        never the binary. Reverting undoes every later checkpoint and is
        itself logged, so it can be reverted right back.
        """

        COLS = ("When", "Name", "Bytes changed", "Status")
        NAME = 1

        def __init__(self, target, parent=None, on_compare=None,
                     on_merge=None):
            super().__init__(parent)
            from . import history as _history
            self._history = _history
            self.target = target
            #: ``on_merge(ids, name) -> id``: merge somewhere else (a
            #: project's history) instead of in the file's own log
            self._on_merge = on_merge
            #: called with a version id to show that version as the tab's
            #: comparison (None: no such button, e.g. no tab to show it in)
            self._on_compare = on_compare
            self.setWindowTitle("Version History — %s" % target.binfile.name)
            self.setWindowIcon(app_icon())
            self.resize(820, 460)
            self._versions = []
            self._filling = False

            v = QtWidgets.QVBoxLayout(self)
            self.lead = QtWidgets.QLabel()
            self.lead.setWordWrap(True)
            v.addWidget(self.lead)

            self.tbl = QtWidgets.QTableWidget(0, len(self.COLS))
            self.tbl.setHorizontalHeaderLabels(list(self.COLS))
            self.tbl.verticalHeader().setVisible(False)
            self.tbl.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows)
            self.tbl.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection)
            self.tbl.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
                | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed)
            self.tbl.itemChanged.connect(self._renamed)
            self.tbl.itemSelectionChanged.connect(self._sync_buttons)
            v.addWidget(self.tbl, 1)

            hint = self.hint = QtWidgets.QLabel(
                "Double-click a name (or press F2) to rename a checkpoint. "
                "Select several adjacent checkpoints to merge them into one.")
            hint.setStyleSheet("color: gray")
            hint.setWordWrap(True)
            v.addWidget(hint)

            btns = QtWidgets.QHBoxLayout()
            self.rename_btn = QtWidgets.QPushButton("Rename")
            self.rename_btn.clicked.connect(self._edit_name)
            btns.addWidget(self.rename_btn)
            self.merge_btn = QtWidgets.QPushButton("Merge selected...")
            self.merge_btn.clicked.connect(self._merge)
            btns.addWidget(self.merge_btn)
            btns.addStretch(1)
            self.compare_btn = QtWidgets.QPushButton("Compare with selected")
            self.compare_btn.setToolTip(
                "Show the selected version as this tab's comparison. The file "
                "itself is not changed.")
            self.compare_btn.clicked.connect(self._compare)
            self.compare_btn.setVisible(on_compare is not None)
            btns.addWidget(self.compare_btn)
            self.revert_btn = QtWidgets.QPushButton("Revert to selected")
            self.revert_btn.clicked.connect(self._revert)
            btns.addWidget(self.revert_btn)
            close = QtWidgets.QPushButton("Close")
            close.clicked.connect(self.reject)
            btns.addWidget(close)
            v.addLayout(btns)
            self._fill()
            if len(self._versions) > 1:
                self.tbl.selectRow(1)
            elif self._versions:
                self.tbl.selectRow(0)
            self._sync_buttons()

        # -- the list --------------------------------------------------
        def _fill(self, keep=()):
            self._filling = True
            self._versions = self._history.list_versions(self.target)
            n = len(self._versions)
            self.lead.setText(
                "<b>%d checkpoint(s)</b>, newest first. Reverting restores "
                "the bytes right after the selected save." % n if n else
                "No checkpoints yet -- every save of this branch adds one.")
            self.tbl.setRowCount(n)
            for i, ver in enumerate(self._versions):
                when = ver.get("ts", "")
                if ver.get("ts_from") and ver.get("ts_from") != when:
                    when = "%s → %s" % (ver["ts_from"], when)
                when = when.replace("T", " ")
                cells = [when, self._history.display_label(ver),
                         str(ver.get("n_bytes", 0)),
                         "pending (unsaved)" if ver.get("pending") else "saved"]
                tip = ["Automatic label: %s" % ver.get("label", "")]
                if ver.get("regions"):
                    tip.append("Also changed: %s" % ", ".join(
                        sorted(ver["regions"])))
                if ver.get("merged_from"):
                    tip.append("Merged from %d saves" % len(ver["merged_from"]))
                for j, txt in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(txt)
                    flags = it.flags()
                    if j != self.NAME:
                        flags &= ~QtCore.Qt.ItemFlag.ItemIsEditable
                    it.setFlags(flags)
                    it.setToolTip("\n".join(tip))
                    if j == 0:
                        it.setData(QtCore.Qt.ItemDataRole.UserRole, ver["id"])
                    self.tbl.setItem(i, j, it)
            self.tbl.resizeColumnsToContents()
            self.tbl.horizontalHeader().setSectionResizeMode(
                self.NAME, QtWidgets.QHeaderView.ResizeMode.Stretch)
            if keep:
                sm = self.tbl.selectionModel()
                for i, ver in enumerate(self._versions):
                    if ver["id"] in keep:
                        sm.select(self.tbl.model().index(i, 0),
                                  QtCore.QItemSelectionModel.SelectionFlag.Select
                                  | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            self._filling = False

        def _rows(self):
            return sorted({ix.row() for ix in
                           self.tbl.selectionModel().selectedRows()})

        def _sync_buttons(self):
            rows = self._rows()
            self.rename_btn.setEnabled(len(rows) == 1)
            self.revert_btn.setEnabled(len(rows) == 1)
            self.compare_btn.setEnabled(len(rows) == 1)
            self.merge_btn.setEnabled(len(rows) >= 2)

        # -- naming ----------------------------------------------------
        def _edit_name(self):
            rows = self._rows()
            if len(rows) == 1:
                self.tbl.editItem(self.tbl.item(rows[0], self.NAME))

        def _renamed(self, item):
            if self._filling or item.column() != self.NAME:
                return
            ver = self._versions[item.row()]
            text = item.text().strip()
            if text == self._history.display_label(ver):
                return
            try:
                self._history.set_label(self.target, ver["id"], text)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, "Rename", str(exc))
            self._fill(keep={ver["id"]})

        # -- merging ---------------------------------------------------
        def _merge(self):
            rows = self._rows()
            picked = [self._versions[r] for r in rows]
            names = [v["name"] for v in reversed(picked) if v.get("name")]
            name, ok = QtWidgets.QInputDialog.getText(
                self, "Merge versions",
                "Merge %d checkpoints into one?\n\nOnly the history changes; "
                "the binary is not touched, and reverting still restores the "
                "same bytes.\n\nName for the merged checkpoint:" % len(picked),
                QtWidgets.QLineEdit.EchoMode.Normal, "; ".join(names))
            if not ok:
                return
            if any(v.get("pending") for v in picked):
                QtWidgets.QMessageBox.information(
                    self, "Merge versions", "Unsaved changes are not a version "
                    "yet: save first, then merge.")
                return
            try:
                if self._on_merge is not None:
                    new_id = self._on_merge([v["id"] for v in picked], name)
                else:
                    new_id = self._history.squash_versions(
                        self.target, [v["id"] for v in picked], name)["id"]
            except (OSError, ValueError, KeyError) as exc:
                QtWidgets.QMessageBox.information(self, "Merge versions",
                                                  str(exc))
                return
            self._fill(keep={new_id})
            self._sync_buttons()

        # -- comparing -------------------------------------------------
        def _compare(self):
            rows = self._rows()
            if len(rows) != 1 or self._on_compare is None:
                return
            if self._on_compare(self._versions[rows[0]]["id"]):
                self.accept()

        # -- reverting -------------------------------------------------
        def _revert(self):
            rows = self._rows()
            if len(rows) != 1:
                return
            ver = self._versions[rows[0]]
            if rows[0] == 0:
                QtWidgets.QMessageBox.information(
                    self, "Version History", "This is already the latest version.")
                return
            if QtWidgets.QMessageBox.question(
                    self, "Revert to Version",
                    "Revert to \"%s\" (saved %s)?\n\n"
                    "Every checkpoint after this one will be undone."
                    % (self._history.display_label(ver), ver.get("ts", "?"))
                    ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            rv = self._history.revert_to_version(self.target, ver["id"])
            lines = ["Reverted %d checkpoint(s), %d byte(s) restored."
                     % (rv.n_versions, rv.n_bytes)]
            if rv.warnings:
                lines.append("")
                lines.extend("  %s" % w for w in rv.warnings[:20])
                if len(rv.warnings) > 20:
                    lines.append("  ...and %d more" % (len(rv.warnings) - 20))
            QtWidgets.QMessageBox.information(self, "Revert to Version",
                                              "\n".join(lines))
            self.accept()


    class HistoryPatchDialog(QtWidgets.QDialog):
        """Pick two points in the currently loaded binary's OWN automatic
        version history and open the same checklist used for a two-file
        comparison (``PatchBuilderDialog``) between them.

        This is the version-history counterpart to "Generate patch..." --
        same output, same checklist, but the two sides come from
        ``ovtsuite.patch.sessions_from_history`` (reconstructed purely from
        the change log) instead of a separately loaded comparison binary.
        """

        def __init__(self, target, parent=None):
            super().__init__(parent)
            from . import history as _history
            from . import patch as _patch
            self._history = _history
            self._patch = _patch
            self.target = target
            self.setWindowTitle("Generate Patch from History — %s"
                                % target.binfile.name)
            self.setWindowIcon(app_icon())

            self._versions = _history.list_versions(target)  # newest first

            v = QtWidgets.QVBoxLayout(self)
            v.addWidget(QtWidgets.QLabel(
                "Build a patch between two versions of this branch, instead "
                "of a separate comparison binary."))

            form = QtWidgets.QFormLayout()
            self.from_box = QtWidgets.QComboBox()
            self.to_box = QtWidgets.QComboBox()
            # the state before the oldest checkpoint: in a project tab the
            # original as read (unless the history the tab sees starts later)
            self.from_box.addItem(self._original_label(parent),
                                  _history.ORIGINAL)
            # self._versions is newest-first; show oldest-first in the pickers
            # so "From" naturally reads earlier than "To".
            for ver in reversed(self._versions):
                label = "%s -- %s%s" % (
                    ver.get("ts", "?"), _history.display_label(ver),
                    "  (pending, unsaved)" if ver.get("pending") else "")
                self.from_box.addItem(label, ver["id"])
                self.to_box.addItem(label, ver["id"])
            self.to_box.addItem("Current (live, unsaved edits included)", None)
            self.from_box.setCurrentIndex(0)
            self.to_box.setCurrentIndex(self.to_box.count() - 1)
            form.addRow("From version:", self.from_box)
            form.addRow("To version:", self.to_box)
            v.addLayout(form)

            btns = QtWidgets.QHBoxLayout()
            btns.addStretch(1)
            nxt = QtWidgets.QPushButton("Next...")
            nxt.clicked.connect(self._next)
            btns.addWidget(nxt)
            cancel = QtWidgets.QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btns.addWidget(cancel)
            v.addLayout(btns)

        def _original_label(self, ws):
            ref = getattr(ws, "project_ref", None)
            if ref is not None:
                try:
                    s = ref.set
                    head = s.branch(ref.bid).head
                    if len(list(s.lineage(head))) - 1 == len(
                            [v for v in self._versions if not v.get("pending")]):
                        return "The original as read"
                except Exception:
                    pass
                return "Before the oldest version listed"
            return "As first opened (before the oldest version)"

        def _next(self):
            older_id = self.from_box.currentData()
            newer_id = self.to_box.currentData()
            if older_id == newer_id:
                QtWidgets.QMessageBox.information(
                    self, "Generate Patch from History",
                    "Pick two different versions.")
                return
            # currentData() gives None for "Current" already; if the same
            # version id was picked for both by an id match (not the
            # sentinel), the check above already caught it.
            try:
                older, newer = self._patch.sessions_from_history(
                    self.target, older_id, newer_id)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Generate Patch from History", str(exc))
                return
            self.accept()
            dlg = PatchBuilderDialog(older, newer, self.parent())
            dlg.exec()


    #: Short badge and tint for each platform's tab. ECU blue, TCU amber;
    #: anything else a neutral green so it never reads as either.
    PLATFORM_BADGE = {
        "edc16": ("ECU", "EDC16", QtGui.QColor(61, 124, 201)),
        "edc15": ("ECU", "EDC15", QtGui.QColor(61, 124, 201)),
        "zf5hp": ("TCU", "ZF 5HP", QtGui.QColor(224, 190, 64)),
        "zf6hp": ("TCU", "ZF 6HP", QtGui.QColor(224, 160, 48)),
        "zf8hp": ("TCU", "ZF 8HP", QtGui.QColor(224, 120, 48)),
        "hvac": ("HVAC", "Climatronic", QtGui.QColor(90, 160, 110)),
        "vdo": ("CLU", "VDO cluster", QtGui.QColor(150, 110, 190)),
        "aisin": ("TCU", "Aisin", QtGui.QColor(200, 90, 70)),
        "unknown": ("BIN", "Unrecognised binary", QtGui.QColor(128, 128, 128)),
    }

    def platform_badge(platform):
        """(badge, long name, tint colour) for a session platform."""
        return PLATFORM_BADGE.get(platform or "",
                                  ((platform or "?").upper(), platform or "?",
                                   QtGui.QColor(128, 128, 128)))

    def _elide(name: str, cap: int = 34) -> str:
        """Middle-elide a file name so a tab never grows past a readable width;
        the full name is always in the tooltip."""
        if len(name) <= cap:
            return name
        keep = cap - 1
        return name[:keep - keep // 2] + "…" + name[-(keep // 2):]

    def _path_key(path) -> Optional[str]:
        """A path normalised for "is this file already open" comparisons."""
        if not path:
            return None
        try:
            return _os.path.normcase(_os.path.realpath(path))
        except (OSError, ValueError):
            return _os.path.normcase(_os.path.abspath(path))

    class Workspace(QtWidgets.QWidget):
        """One tab: a primary binary, an optional comparison, and everything
        that shows or edits them -- the parameter tree, the table, the edit
        bar and the tab's own tool windows.

        The MainWindow around it owns only the menus, the tab bar and the
        status bar, and routes every menu action to the active tab. Nothing
        here reaches into another tab; the one cross-tab relationship (a
        comparison that is another tab's primary) is held as ``cmp_link`` and
        kept in step by :meth:`poll`.
        """

        #: The tool windows that belong to a tab. Non-modal; one of each kind
        #: per tab; hidden while the tab is not the active one.
        TOOL_KINDS = ("dtc", "msblock", "gears", "vss", "hex", "logtune")

        def __init__(self, win, primary: Optional[TunerSession] = None,
                     comparison: Optional[TunerSession] = None):
            super().__init__()
            self.win = win
            self.primary = primary
            self.comparison = comparison
            #: set when this tab shows a branch of an .ovt project
            #: (:class:`ovtsuite.gui_project.ProjectRef`); None otherwise
            self.project_ref = None
            self._untitled = False
            self._backup_path = None
            self._eeprom = None
            self._eeprom_dirty = False
            self._diff_dlg = None
            #: The tab whose primary is this tab's comparison -- a live link,
            #: so its edits show here. None for a comparison read from disk.
            self.cmp_link = None
            #: True once the linked tab has gone (closed, or it opened another
            #: file): the comparison stays exactly as it last was.
            self.cmp_frozen = False
            #: When the comparison is one of the primary's own saved versions
            #: (Compare with saved version): {"path", "id", "label", "ts"}.
            #: It lives only in memory, so this is what remembers it.
            self.cmp_version = None
            #: kind -> this tab's open tool window (see TOOL_KINDS)
            self._tools = {}
            #: tool windows hidden because the tab was switched away from
            self._suspended = []
            #: kind -> how that tool window was made, to rebuild it when the
            #: comparison it was given changes
            self._factories = {}
            self._seen_rev = None
            self._seen_cmp_rev = None
            self._seen_dirty = False
            #: set while tabs are being reopened: no "file loaded" popups
            self._quiet = False
            self.tree = QtWidgets.QTreeWidget()
            self.tree.setHeaderLabels(["Parameter", "A2L symbol",
                                       "Description"])
            self.tree.setColumnWidth(0, 320)
            self.tree.setColumnWidth(1, 200)
            self.tree.itemSelectionChanged.connect(self._on_select)
            self.tree.itemDoubleClicked.connect(self._on_tree_double)
            self._tree_middle = _TreeMiddleClick(self.tree, self._on_tree_middle)
            from . import gui_paramenu as _gpm
            _gpm.attach(self)               # right-click: the parameter menu
            self.tree.viewport().installEventFilter(self._tree_middle)

            self.filter = QtWidgets.QLineEdit()
            self.filter.setPlaceholderText("Filter parameters...")
            self.filter.setToolTip(
                "Keep the parameters whose name, symbol, description or "
                "coding contains this text.\n* matches any run of characters, "
                "? any one character (accped*map).")
            self.filter.textChanged.connect(self._populate)

            # Task-group filter. Filled from the groups the open file
            # actually has (an EDC16 has no "Shift points"), in _build_tree.
            self.tier_box = QtWidgets.QComboBox()
            self.tier_box.addItem("All groups", 0)
            self.tier_box.currentIndexChanged.connect(self._populate)
            # Supporting and plumbing groups (Other engine maps, Sensors &
            # models, Internals ...) stay out of the way until asked for.
            self.advanced = QtWidgets.QCheckBox("Advanced")
            self.advanced.setToolTip(
                "Also show the supporting groups: Other engine maps, "
                "Sensors & models, Internals / OBD, Adaptation and "
                "Unverified placement.\nPicking one of them in the group "
                "list shows it either way.")
            try:
                self.advanced.setChecked(bool(_settings().value(
                    ADVANCED_KEY, False, type=bool)))
            except Exception:
                pass
            self.advanced.toggled.connect(self._on_advanced)

            left = QtWidgets.QWidget()
            lv = QtWidgets.QVBoxLayout(left)
            lv.setContentsMargins(4, 4, 4, 4)
            grp_row = QtWidgets.QHBoxLayout()
            grp_row.addWidget(self.tier_box, 1)
            grp_row.addWidget(self.advanced)
            lv.addLayout(grp_row)
            lv.addWidget(self.filter)
            self.diff_only = QtWidgets.QCheckBox(
                "Show only parameters that differ from the comparison")
            self.diff_only.setEnabled(comparison is not None)
            self.diff_only.toggled.connect(self._populate)
            lv.addWidget(self.diff_only)
            lv.addWidget(self.tree)
            self._changed_cache = None
            self._changed_token = None

            # The tables open in this file tab, one tab each, over the one
            # grid. The tab that is showing is _tt_cur; each tab's data is a
            # _TableTab holding what that table remembers (heatmap, Z mode,
            # scroll and selection). A click in the tree opens a table in the
            # italic PREVIEW tab, which the next click reuses; a double-click,
            # an edit, or double-clicking the tab keeps it open.
            self.ttabs = TableTabBar()
            self.ttabs.currentChanged.connect(self._on_table_tab)
            self.ttabs.tabCloseRequested.connect(self.close_table)
            self.ttabs.tabBarDoubleClicked.connect(self.pin_table)
            self.ttabs.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.ttabs.customContextMenuRequested.connect(self._table_tab_menu)
            self.ttabs.setVisible(False)
            self._tt_cur = None
            self._tt_quiet = False      # set while the tree is moved to match
            self._heat_default = False  # what a newly opened table starts with

            self.model = TableModel(primary)
            self.table = QtWidgets.QTableView()
            self.table.setModel(self.model)
            from . import gui_address as _gaddr  # the status bar's address
            _gaddr.watch(self)
            self.table.setItemDelegate(OptionsDelegate(self.table))
            self.table.setAlternatingRowColors(True)
            self.table.horizontalHeader().setMinimumSectionSize(_MIN_CELL_W)
            self.table.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self._table_menu)
            self.table.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection)
            self.table.setEditTriggers(
                QtWidgets.QAbstractItemView.DoubleClicked
                | QtWidgets.QAbstractItemView.EditKeyPressed
                | QtWidgets.QAbstractItemView.AnyKeyPressed)
            # Ctrl+wheel / Ctrl+= / Ctrl+- / Ctrl+0, remembered (gui_zoom)
            from . import gui_zoom as _gz
            self._zoom = _gz.install(
                self.table, "table",
                after=lambda _f: self.table.resizeColumnsToContents())
            from . import gui_cellkeys as _gck      # cell keys (Edit > Cells)
            _gck.attach(self)
            from . import gui_tablesel as _gts      # one half selected at once
            _gts.attach(self)

            self.title = QtWidgets.QLabel("Select a parameter")
            self.title.setStyleSheet("font-weight: bold;")
            self.title.setWordWrap(True)    # a long name wraps, never widens
            self.desc = QtWidgets.QLabel("")
            self.desc.setWordWrap(True)
            self.desc.setStyleSheet("color: gray;")

            # selection-scoped modify bar (6HP-style): op + value applied to the
            # highlighted cells only, in one undoable step.
            self.editbar = QtWidgets.QWidget()
            eb = _GroupedBar(self.editbar)
            eb.setContentsMargins(0, 0, 0, 0)
            eb.addWidget(QtWidgets.QLabel("Modify selection:"))
            self.op_box = QtWidgets.QComboBox()
            for lbl, key in (("Set =", "set"), ("Add +", "add"),
                             ("Subtract −", "sub"), ("Multiply ×", "mul"),
                             ("Divide ÷", "div")):
                self.op_box.addItem(lbl, key)
            self.op_box.setCurrentIndex(3)          # Multiply by default
            eb.addWidget(self.op_box)
            self.op_val = QtWidgets.QLineEdit("1.05")
            self.op_val.setMaximumWidth(110)
            self.op_val.returnPressed.connect(self._apply_op)
            eb.addWidget(self.op_val)
            apply_btn = QtWidgets.QPushButton("Apply")
            apply_btn.clicked.connect(self._apply_op)
            eb.addWidget(apply_btn)

            sep = QtWidgets.QFrame()
            sep.setFrameShape(QtWidgets.QFrame.VLine)
            eb.addWidget(sep)
            eb.addWidget(QtWidgets.QLabel("Fill:"))
            for lbl, mode, tip in (
                    ("↔", "h", "Interpolate each row between the left and "
                     "right edges of the selection"),
                    ("↕", "v", "Interpolate each column between the top and "
                     "bottom edges of the selection"),
                    ("▦", "2d", "Bilinear-interpolate the selected block from "
                     "its four corners")):
                b = QtWidgets.QPushButton(lbl)
                b.setMaximumWidth(34)
                b.setToolTip(tip)
                b.clicked.connect(lambda _c=False, m=mode: self._interp(m))
                eb.addWidget(b)
            sm = QtWidgets.QPushButton("Smooth")
            sm.setToolTip("One neighbour-averaging smoothing pass over the "
                          "selected cells")
            sm.clicked.connect(self._smooth_sel)
            eb.addWidget(sm)

            eb.addStretch(1)
            # ZF 6HP: shift, TCC and tip-limit maps store output-shaft speed.
            # Hidden entirely on every other kind of map, because offering
            # "Engine speed (RPM)" for a pressure map would be nonsense.
            self.zmode_lbl = QtWidgets.QLabel("Show Z as:")
            self.zmode_box = QtWidgets.QComboBox()
            for m in _view6.Z_MODES:
                self.zmode_box.addItem(_view6.Z_MODE_LABELS[m], m)
            self.zmode_box.setToolTip(
                "How to display this map's stored output-shaft speed. Edits "
                "you type are converted back, so the stored value is never "
                "changed by looking at it.\n\n"
                + _view6.ENGINE_MODE_CAVEAT)
            self.zmode_box.currentIndexChanged.connect(self._zmode_changed)
            self.vehicle_btn = QtWidgets.QPushButton("Vehicle…")
            self.vehicle_btn.setToolTip(
                "Final-drive ratio and tyre size, for the road-speed modes")
            self.vehicle_btn.clicked.connect(self.open_vehicle)
            self.mirror_btn = QtWidgets.QPushButton("Copy to other bank")
            self.mirror_btn.setToolTip(
                "Overwrite this parameter's other coding bank with these "
                "values. The two banks are separate calibrations, not copies, "
                "so you are asked to confirm how many cells will change.")
            self.mirror_btn.clicked.connect(self.mirror_to_bank)
            for w in (self.zmode_lbl, self.zmode_box, self.vehicle_btn,
                      self.mirror_btn):
                w.setVisible(False)
                eb.addWidget(w)
            self._zmode_pref = str(_settings().value("zf6hp/zmode",
                                                     _view6.DEFAULT_MODE))
            if self._zmode_pref not in _view6.Z_MODES:
                self._zmode_pref = _view6.DEFAULT_MODE
            self._zmode_default = self._zmode_pref
            self.model.modelReset.connect(self._sync_zmode)
            self.model.edited.connect(self._pin_shown)

            self.heat_chk = QtWidgets.QCheckBox("Heatmap")
            self.heat_chk.setToolTip("Shade cells by value (blue = low, "
                                     "red = high)")
            self.heat_chk.toggled.connect(self._heat_toggled)
            eb.addStretch()                 # its own group: wraps as a pair
            eb.addWidget(self.heat_chk)
            self.surf_btn = QtWidgets.QPushButton("3D surface")
            self.surf_btn.setShortcut("Ctrl+3")     # a toggle: gui_surface3d
            eb.addWidget(self.surf_btn)
            self.dyno_btn = QtWidgets.QPushButton("Dyno")
            self.dyno_btn.setShortcut("Ctrl+4")     # a toggle: gui_dyno
            eb.addWidget(self.dyno_btn)

            # the comparison (when loaded) is shown INSIDE this same grid,
            # under a banner row, so both share one set of scrollers and one
            # column layout; the banner row is spanned across the grid.
            self.model.modelReset.connect(self._apply_spans)
            if comparison is not None:
                self.model.cmp_name = comparison.binfile.name

            right = QtWidgets.QWidget()
            rv = QtWidgets.QVBoxLayout(right)
            rv.addWidget(self.ttabs)
            rv.addWidget(self.title)
            rv.addWidget(self.desc)
            rv.addWidget(self.editbar)
            from . import gui_surface3d as _s3d     # 3-D surface above it
            from . import gui_dyno as _gdy          # the dyno chart above that
            from . import gui_panelayout as _gpl    # ... or beside: an option
            rv.addWidget(_gpl.install(self, _gdy.attach(self, _s3d.attach(self))), 1)

            split = QtWidgets.QSplitter()
            split.addWidget(left)
            split.addWidget(right)
            split.setStretchFactor(1, 1)
            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            from . import gui_start as _gs      # an empty tab: the start page
            lay.addWidget(_gs.wrap(self, split))
            self._populate()

        # -- the window around this tab -----------------------------------
        # The per-file code below was written against a QMainWindow; these
        # route its window-level calls to the MainWindow that holds the tab.
        def statusBar(self):
            return self.win.statusBar()

        def _rebuild_recent(self):
            self.win._rebuild_recent()

        def _rebuild_recent_comparisons(self):
            self.win._rebuild_recent_comparisons()

        def _refresh_enabled(self):
            self.win._refresh_enabled()

        def _status(self):
            self.win._workspace_changed(self)

        def is_active(self) -> bool:
            return self.win.current is self

        def raise_(self):
            """Dialogs bring their owner forward with ``parent().raise_()``;
            for a tab that means selecting the tab as well as the window."""
            self.win.show_workspace(self)
            self.win.raise_()

        def activateWindow(self):
            self.win.activateWindow()

        # -- what this tab holds ------------------------------------------
        def is_empty(self) -> bool:
            return self.primary is None

        def is_dirty(self) -> bool:
            if getattr(self, "view_only", None):
                return False            # a saved version, read-only
            return self.primary is not None and bool(
                self.primary.modified or self._eeprom_dirty)

        def source_path(self) -> Optional[str]:
            """The file this tab's primary came from; None for a read from a
            module that has not been saved anywhere yet."""
            if self.primary is None or self._untitled \
                    or getattr(self, "view_only", None):
                return None             # nothing of its own on disk
            if self.project_ref is not None:
                return self.project_ref.ref()   # the .ovt + set + branch
            return self._backup_path or getattr(self.primary, "path", None)

        def comparison_path(self) -> Optional[str]:
            """The comparison's file on disk (whatever way it was loaded)."""
            if self.comparison is None or self.cmp_version is not None:
                return None             # a saved version has no file of its own
            return getattr(self.comparison, "path", None)

        def platform(self) -> Optional[str]:
            return getattr(self.primary, "platform", None)

        def display_name(self) -> str:
            if self.primary is None:
                return "Start"
            return self.primary.binfile.name

        def tab_label(self) -> str:
            if self.primary is None:
                return "Start"
            badge = platform_badge(self.primary.platform)[0]
            text = "%s  %s%s" % (badge, _elide(self.primary.binfile.name),
                                 "*" if self.is_dirty() else "")
            if self.comparison is not None:
                text += "  ↔  %s" % _elide(self.comparison.binfile.name, 24)
                if self.cmp_frozen:
                    text += " (closed tab)"
            if self.project_ref is not None:
                return _gp.tab_label(self, text)
            return text

        def tab_tooltip(self) -> str:
            if self.primary is None:
                return ("Empty tab — File ▸ Open project (Ctrl+O), Open "
                        "Recent, or Import (Ctrl+Shift+I) -- or drop a file "
                        "here")
            _b, long_name, _c = platform_badge(self.primary.platform)
            lines = ["%s — %s" % (long_name, self.primary.family
                                  or "no definition (hex editor)"),
                     "Primary: %s" % (self.source_path()
                                      or self.primary.binfile.name)]
            if self.is_dirty():
                lines.append("Unsaved changes")
            if self.comparison is not None:
                how = ""
                if self.cmp_link is not None:
                    how = "  (live: follows the tab it is open in)"
                elif self.cmp_frozen:
                    how = "  (its tab was closed; frozen as it last was)"
                lines.append("Comparison: %s%s" % (
                    self.comparison_path() or self.comparison.binfile.name,
                    how))
            if self.project_ref is not None:
                return _gp.tab_tooltip(self, "\n".join(lines))
            return "\n".join(lines)

        def tint(self):
            if self.primary is None:
                return None
            return platform_badge(self.primary.platform)[2]

        def status_text(self) -> str:
            if self.primary is None:
                return ("Nothing open — File ▸ Open project (Ctrl+O), Open "
                        "Recent, Import (Ctrl+Shift+I) or drop a file on the "
                        "window (Ctrl+Shift+T reopens the last closed project)")
            n = len(self.primary.entities)
            dirty = "*" if self.is_dirty() else ""
            cmp_ = ""
            if self.comparison is not None:
                cmp_ = " | compare: %s" % self.comparison.binfile.name
                if self.cmp_link is not None:
                    cmp_ += " (live from tab)"
                elif self.cmp_frozen:
                    cmp_ += " (tab closed)"
            if self._eeprom is not None:
                cmp_ += " | EEPROM loaded%s" % ("*" if self._eeprom_dirty
                                                else "")
            csum = ""
            if (getattr(self.primary, "_checksum_cache", None) is not None
                    and not self.primary.checksum_status_known()):
                # Verifying sums the whole image. After an edit it waits for
                # a pause rather than holding up every keystroke; the first
                # report (on opening) is still made straight away.
                self._schedule_checksum()
                csum = "  checksums: checking..."
            else:
                rep = self.primary.checksum_status()
                if rep is not None:
                    csum = "  checksums: %s" % (
                        "CANNOT VERIFY" if getattr(rep, "unreadable", "")
                        else "valid" if rep.ok else "NEED FIX (auto on save)")
                elif any(getattr(d, "v3", False)
                         for d in getattr(self.primary, "datasets", None) or ()):
                    # MSA15 / EDC15V software whose checksum routine the app
                    # does not read: say so rather than nothing (Verify
                    # checksums explains)
                    csum = "  checksums: NOT CHECKED"
            fam = self.primary.family
            if getattr(self.primary, "is_edc15", False):
                v3 = any(getattr(d, "v3", False) for d in self.primary.datasets)
                fam = "%s %s, %d datasets" % ("MSA15/EDC15V" if v3 else "EDC15", fam,
                                              len(self.primary.datasets))
            return "%s%s  [%s, %d parameters]%s%s" % (
                self.primary.binfile.name, dirty, fam, n, cmp_, csum)

        def _schedule_checksum(self):
            """Verify the checksums once edits pause, then redraw the status
            line (see status_text)."""
            t = getattr(self, "_csum_timer", None)
            if t is None:
                t = self._csum_timer = QtCore.QTimer(self)
                t.setSingleShot(True)
                t.setInterval(CHECKSUM_DELAY_MS)
                t.timeout.connect(self._checksum_due)
            t.start()                       # restarted by every edit

        def _checksum_due(self):
            if self.primary is not None:
                self.primary.checksum_status()
            self._status()

        # -- comparison -------------------------------------------------
        def set_comparison(self, comp, link=None):
            """Make ``comp`` this tab's comparison. ``link`` is the tab whose
            primary it is, for a live link that follows that tab's edits;
            None for one read from disk. ``comp=None`` drops it."""
            old_dlg = self._diff_dlg
            if old_dlg is not None:
                old_dlg.close()             # its rows describe the old pair
                self._diff_dlg = None
            self.comparison = comp
            self.cmp_link = link if comp is not None else None
            self.cmp_version = getattr(comp, "version_of", None) \
                if comp is not None else None
            self.cmp_frozen = False
            self._changed_cache = None
            self._seen_cmp_rev = comp.revision if comp is not None else None
            self.diff_only.blockSignals(True)
            if comp is None:
                self.diff_only.setChecked(False)
            self.diff_only.setEnabled(comp is not None)
            self.diff_only.blockSignals(False)
            self._show_comparison(None, None)
            self._repopulate_keep()
            self._reopen_tools()
            self._status()
            self._refresh_enabled()

        def _reopen_tools(self):
            """Tool windows are handed the comparison when they are made, so
            one that is open when the comparison changes is rebuilt."""
            for kind in [k for k in self._tools if k != "logtune"]:
                dlg = self._tools.pop(kind)
                was_hidden = dlg in self._suspended
                try:
                    geo = dlg.saveGeometry()
                    dlg.finished.disconnect()
                    dlg.close()
                except RuntimeError:
                    continue
                if was_hidden:
                    self._suspended.remove(dlg)
                new = self.open_tool(kind, self._factories[kind])
                if new is not None:
                    new.restoreGeometry(geo)
                    if was_hidden:
                        new.hide()
                        self._suspended.append(new)

        def pair_problem(self, comp) -> Optional[str]:
            """Why ``comp`` cannot be this tab's comparison, or None.

            Parameters pair up through the platform's own knowledge base, so a
            comparison of another platform has nothing to line up against --
            refused outright rather than shown as a screen of blanks."""
            if self.primary is None or comp is None:
                return None
            if comp.platform == self.primary.platform:
                return None
            _pb, pname, _pc = platform_badge(self.primary.platform)
            _cb, cname, _cc = platform_badge(comp.platform)
            return ("A comparison has to be the same platform as the primary."
                    "\n\nThis tab's primary, %s, is %s; %s is %s."
                    % (self.primary.binfile.name, pname,
                       comp.binfile.name, cname))

        def _repopulate_keep(self):
            """Rebuild the tree and put the same parameter back on screen."""
            e = self.model.entity
            self._populate()
            if e is not None:
                self._reselect(e)

        def _reselect(self, e=None):
            """Show ``e`` (default: what is on screen) again, so a new or
            changed comparison is laid over it."""
            e = e if e is not None else self.model.entity
            if e is None or self.primary is None:
                return
            t = self._tt_cur
            if t is not None and t.entity is e:
                self._save_view()
                self._show_table(t)         # redrawn where it was
            else:
                self.open_table(e)

        # -- keeping in step --------------------------------------------
        def poll(self):
            """Follow edits made anywhere -- the grid, a tool window, undo, a
            patch, or (for a live comparison) the other tab. Run from the
            window's timer; nothing happens unless a revision moved."""
            p = self.primary
            if p is not None:
                rev = p.revision
                if rev != self._seen_rev:
                    self._seen_rev = rev
                    self._refresh_set_labels()
                    if (self.model.entity is not None
                            and getattr(self.model, "_rev", None) != rev):
                        self.model.refresh()
                    self._refresh_tools()
                    self._status()
            # a save marks the file clean without moving the revision, so
            # the "*" follows the saved state itself, whoever saved
            dirty = self.is_dirty()
            if dirty != self._seen_dirty:
                self._seen_dirty = dirty
                self._status()
            link = self.cmp_link
            if link is not None and link.primary is not self.comparison:
                self.freeze_comparison()    # that tab opened another file
            c = self.comparison
            if c is not None and c.revision != self._seen_cmp_rev:
                self._seen_cmp_rev = c.revision
                self._changed_cache = None
                if self.diff_only.isChecked():
                    self._repopulate_keep()
                else:
                    self._reselect()
                self._status()

        def freeze_comparison(self):
            """The linked tab is gone: keep its last state, stop following."""
            if self.cmp_link is None:
                return
            self.cmp_link = None
            self.cmp_frozen = True
            self._status()

        # -- copying from another tab -------------------------------------
        def _selected_z(self):
            """(r, c) of the selected table cells, entity coordinates."""
            out = set()
            for ix in self._selected_indexes():
                z = self.model.z_at(ix)
                if z is not None:
                    out.add(z)
            return out

        def copy_source(self, other):
            """(entity in ``other``, None) when the parameter on screen can be
            copied from that tab, else (None, reason)."""
            e = self.model.entity
            if e is None or self.primary is None:
                return None, "no parameter selected"
            if other is self or other.primary is None:
                return None, "empty"
            if other.primary.platform != self.primary.platform:
                return None, "different platform"
            src = self.primary.counterpart(other.primary, e)
            if src is None:
                return None, "no %s there" % (e.name or "matching parameter")
            if _cell_pairs(e, src) is None:
                return None, "%s there, %s here" % (_shape_text(src),
                                                    _shape_text(e))
            return src, None

        def copy_from_tab(self, other):
            """Overwrite the selected cells (or the whole table) of the
            parameter on screen with the same parameter from another tab, in
            one undoable step."""
            e = self.model.entity
            src, why = self.copy_source(other)
            if src is None:
                QtWidgets.QMessageBox.information(
                    self, "Copy from tab",
                    "Can't copy %s from %s: %s." % (
                        e.name if e is not None else "this",
                        other.display_name(), why))
                return 0
            sel = self._selected_z()
            cells = []
            for (r, c), (sr, sc) in _cell_pairs(e, src):
                if sel and (r, c) not in sel:
                    continue
                v = src.cell(sr, sc)
                if abs(v - e.cell(r, c)) > 1e-9:
                    cells.append((r, c, v))
            turned = (e.rows, e.cols) != (src.rows, src.cols)
            axes = []
            if not sel:
                # the whole table: the axes decide where each value applies,
                # so values copied onto different breakpoints would move. A
                # run stored the other way round keeps its axis on the
                # other side.
                sx, sy = (src.y, src.x) if turned else (src.x, src.y)
                for ax_e, ax_s in ((e.x, sx), (e.y, sy)):
                    vals_e = e.x_values() if ax_e is e.x else e.y_values()
                    vals_s = (src.x_values() if ax_s is src.x
                              else src.y_values())
                    if ax_e is None or ax_s is None or len(vals_e) != len(vals_s):
                        continue
                    for i, (a, b) in enumerate(zip(vals_e, vals_s)):
                        if abs(a - b) > 1e-9:
                            axes.append((ax_e, i, b))
            copy_axes = False
            if axes:
                r = QtWidgets.QMessageBox.question(
                    self, "Copy from tab",
                    "%s has different axis breakpoints in %s.\n\nCopy the "
                    "axes as well? Without them the copied values sit at this "
                    "file's operating points instead of the ones they were "
                    "calibrated for." % (e.name, other.display_name()),
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.No
                    | QtWidgets.QMessageBox.StandardButton.Cancel)
                if r == QtWidgets.QMessageBox.StandardButton.Cancel:
                    return 0
                copy_axes = r == QtWidgets.QMessageBox.StandardButton.Yes
            if not cells and not (copy_axes and axes):
                self.statusBar().showMessage(
                    "%s already matches %s." % (e.name, other.display_name()),
                    5000)
                return 0
            with self.primary.transaction("copy %s from %s" % (
                    e.name, other.display_name())):
                if cells:
                    self.primary.edit_cells(e, cells)
                if copy_axes:
                    for ax, i, v in axes:
                        self.primary.edit_axis(ax, i, v)
            self._reselect(e)
            self._status()
            self.statusBar().showMessage(
                "Copied %d cell(s)%s of %s from %s." % (
                    len(cells), " and the axes" if copy_axes and axes else "",
                    e.name, other.display_name()), 6000)
            return len(cells)

        def _table_menu(self, pos):
            menu = QtWidgets.QMenu(self)
            menu.addAction("Copy", self._copy_cells)
            paste = menu.addAction("Paste", self._paste_cells)
            paste.setEnabled(self.model.entity is not None)
            menu.addSeparator()
            what = "selected cells" if self._selected_z() else "whole table"
            sub = menu.addMenu("Copy %s from tab" % what)
            self.win.fill_copy_from_menu(sub, self)
            from . import gui_cellkeys as _gck
            _gck.add_to_menu(self.win, menu)       # revert actions
            e = self.model.entity
            if e is not None and e.kind in ("map", "curve") and \
                    getattr(e, "source", "") != "user":
                from . import gui_resize as _gr
                why = _gr.availability(self, e)
                menu.addSeparator()
                rs = menu.addAction("Resize axes...",
                                    lambda: _gr.resize_table(self, e))
                rs.setEnabled(why is None)
                rs.setToolTip(why or "Change how many breakpoints this "
                                     "table has")
                menu.setToolTipsVisible(True)
            from . import gui_eoi as _ge
            if e is not None and _ge.involved(self, e):
                menu.addAction("End of injection...",
                               lambda: _ge.open_for(self, e))
            _gui_ai.extend_menu(self, menu, self.model.entity)
            menu.exec(self.table.viewport().mapToGlobal(pos))

        # -- tool windows (non-modal, one of each per tab) ----------------
        def open_tool(self, kind, factory):
            """Show this tab's ``kind`` tool window, creating it on first use.

            They are non-modal so the grid stays usable beside them, and they
            belong to the tab: hidden while another tab is active, closed with
            the tab."""
            dlg = self._tools.get(kind)
            if dlg is not None:
                try:
                    dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                    return dlg
                except RuntimeError:            # already deleted by Qt
                    self._tools.pop(kind, None)
            dlg = factory()
            if dlg is None:
                return None
            self._factories[kind] = factory
            dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            if self.primary is not None:
                dlg.setWindowTitle("%s — %s" % (
                    dlg.windowTitle(), self.primary.binfile.name))
            self._tools[kind] = dlg
            dlg.finished.connect(
                lambda _r=0, k=kind, d=dlg: self._tool_closed(k, d))
            dlg.show()
            return dlg

        def tool(self, kind):
            """This tab's open ``kind`` tool window, or None."""
            return self._tools.get(kind)

        def _tool_closed(self, kind, dlg):
            if self._tools.get(kind) is dlg:
                del self._tools[kind]
            if dlg in self._suspended:
                self._suspended.remove(dlg)
            if self.primary is not None:
                self.model.refresh()
            self._status()

        def _refresh_tools(self):
            """Let open tool windows show an edit made elsewhere. The window
            the user is typing in made the edit itself and is left alone."""
            active = QtWidgets.QApplication.activeWindow()
            for d in list(self._tools.values()):
                if d is active:
                    continue
                try:
                    if not d.isVisible():
                        continue
                    fn = getattr(d, "refresh_from_session", None)
                    if fn is not None:
                        fn()
                except RuntimeError:
                    pass

        def _owned_windows(self):
            wins = list(self._tools.values())
            if self._diff_dlg is not None:
                wins.append(self._diff_dlg)     # the differences list too
            return wins

        def suspend_tools(self):
            """Hide this tab's tool windows (another tab became active)."""
            for d in self._owned_windows():
                try:
                    if d.isVisible():
                        d.hide()
                        self._suspended.append(d)
                except RuntimeError:
                    pass

        def resume_tools(self):
            """Bring back what :meth:`suspend_tools` hid, up to date."""
            back, self._suspended = self._suspended, []
            for d in back:
                try:
                    fn = getattr(d, "refresh_from_session", None)
                    if fn is not None:
                        fn()
                    d.show()
                except RuntimeError:
                    pass

        def shutdown(self):
            """Close everything this tab owns; it is about to be removed."""
            for d in list(self._tools.values()):
                try:
                    d.close()
                except RuntimeError:
                    pass
            self._tools.clear()
            self._suspended = []
            if self._diff_dlg is not None:
                try:
                    self._diff_dlg.close()
                except RuntimeError:
                    pass
                self._diff_dlg = None

        # -- closing ------------------------------------------------------
        def save_checked(self, tab_only=False) -> bool:
            """Save, reporting a failure instead of raising. True once the
            changes are on disk; False if the user cancelled or it failed.
            ``tab_only``: of a project tab's project, save just this tab."""
            try:
                return bool(self.save(tab_only=tab_only))
            except Exception as ex:                  # disk full, permissions..
                QtWidgets.QMessageBox.critical(
                    self, "Save failed", "%s could not be saved:\n%s"
                    % (self.display_name(), ex))
                return False

        def confirm_close(self, question="Save them before closing the tab?"
                          ) -> bool:
            """Ask about unsaved changes before the file goes. True = go."""
            if self.project_ref is not None and _gp.is_last_tab(self) \
                    and not getattr(self, "_keep_project", False):
                # the project's last tab: close the project (one question)
                return _gp.confirm_close_last(self)
            if not self.is_dirty():
                return True
            self.win.show_workspace(self)
            SB = QtWidgets.QMessageBox.StandardButton
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Warning, "Unsaved changes",
                "%s has unsaved changes." % self.display_name(),
                SB.Save | SB.Discard | SB.Cancel, self)
            box.setInformativeText(question)
            box.setDefaultButton(SB.Save)
            r = box.exec()
            if r == SB.Save:
                return self.save_checked(tab_only=self.project_ref is not None)
            return r == SB.Discard
        # -- menu / actions --------------------------------------------
        def _dim_has_eeprom(self):
            """Whether the loaded .dim (if any) has an EEPROM chunk (index 3)
            -- a cheap existence check with no decode, used to gate "Export
            EEPROM (.dim)..." so it's disabled for a .dim with no EEPROM
            chunk rather than showing an information dialog when clicked."""
            chunks = getattr(self.primary, "_dim_chunks", None) if self.primary else None
            if not chunks:
                return False
            return any(c.index == 3 for c in chunks)

        # -- tree -------------------------------------------------------
        def _changed_set(self):
            """ids of primary entities whose data differs from the comparison
            counterpart (byte-level; fast). Cached until an edit or a new
            comparison invalidates the token."""
            if self.comparison is None:
                return None
            token = (id(self.comparison), self.primary.revision,
                     self.comparison.revision)
            if self._changed_cache is not None \
                    and self._changed_token == token:
                return self._changed_cache
            pdata = self.primary.binfile.data
            cdata = self.comparison.binfile.data
            changed = set()
            for e in (list(self.primary.entities)
                      + list(self.primary.variant_entities())):
                cp = self.primary.counterpart(self.comparison, e)
                if cp is None:
                    continue
                if _same_extent(e, cp):
                    # the axes sit outside [addr, end): a map whose only edit
                    # is its breakpoints differs too
                    if (bytes(pdata[e.addr:e.end]) != bytes(cdata[cp.addr:cp.end])
                            or _axis_changes(e, cp)):
                        changed.add(id(e))
                elif _cells_differ(e, cp):
                    changed.add(id(e))
            self._changed_cache = changed
            self._changed_token = token
            return changed

        def _populate(self):
            """Bring the tree in line with the filters.

            The tree holds EVERY parameter and is built once per file
            (``_build_tree``); the tier box, the text filter and "show only
            parameters that differ" just hide rows (``_apply_tree_filters``).
            Rebuilding ~10k items on every toggle was the slow part."""
            if self.primary is None:
                self.tree.clear()
                self._tree_primary = None
                self._tree_sig = None
                self._item_of = {}
                return
            sig = (id(self.primary.entities), len(self.primary.entities))
            if (getattr(self, "_tree_primary", None) is not self.primary
                    or getattr(self, "_tree_sig", None) != sig):
                self._build_tree()
                self._tree_primary = self.primary
                self._tree_sig = sig
            self._apply_tree_filters()

        def _build_tree(self):
            self.tree.setUpdatesEnabled(False)
            try:
                self.tree.clear()
                tiers: Dict[int, QtWidgets.QTreeWidgetItem] = {}
                cats: Dict[tuple, QtWidgets.QTreeWidgetItem] = {}
                self._item_of = {}
                self._leaf_text = {}
                self._last_needle = ""
                for e in (self.primary.entities
                          + self.primary.variant_entities()):
                    if getattr(e, "_hidden_by", None):
                        continue            # a user parameter stands in for it
                    if e.tier not in tiers:
                        ti = QtWidgets.QTreeWidgetItem(
                            [tier_name(e.tier)])
                        ti.setData(0, _BASE_ROLE, tier_name(e.tier))
                        ti.setToolTip(0, _GROUP_HELP.get(e.tier, ""))
                        ti.setFirstColumnSpanned(True)
                        self.tree.addTopLevelItem(ti)
                        tiers[e.tier] = ti
                    # A ZF entity carries an ordered path -- family, then
                    # coding bank, then driving mode, or clutch role then
                    # shift -- and it nests. Everything else has an unordered
                    # set of memberships from its definition, where only the
                    # first is a heading and nesting them would invent a
                    # hierarchy that is not there.
                    path = list(getattr(e, "category_path", None) or ())
                    if not path:
                        # the definition's own function heading, read as
                        # plain English (title, not code; one language)
                        path = [_catalogue.heading_text(
                            e.categories[0] if e.categories else "",
                            e.name)]
                    parent = tiers[e.tier]
                    for depth in range(len(path)):
                        ck = (e.tier, tuple(path[:depth + 1]))
                        if ck not in cats:
                            # a heading still in German (a supplier's
                            # function title) reads in English
                            head = _tr_de(path[depth])
                            ci = QtWidgets.QTreeWidgetItem([head])
                            ci.setData(0, _BASE_ROLE, head)
                            parent.addChild(ci)
                            cats[ck] = ci
                        parent = cats[ck]
                    label = _english(e)
                    # one line per row: a multi-paragraph description (VDO
                    # fields carry "Values: ..." paragraphs) made rows tall
                    leaf = QtWidgets.QTreeWidgetItem(
                        [label, e.name, " ".join(_parse_desc(e)[0].split())])
                    leaf.setData(0, QtCore.Qt.UserRole, e)
                    # No tooltip text here: building one per cell for every
                    # row cost more than the rest of the tree. _TreeTips
                    # makes it when the pointer is actually over a row.
                    parent.addChild(leaf)
                    self._item_of[id(e)] = leaf
                    self._leaf_text[id(e)] = _filter_text(e, label)
                if getattr(self, "_tree_tips", None) is None:
                    self._tree_tips = _TreeTips(self.tree)
                    self.tree.viewport().installEventFilter(self._tree_tips)
                self._refresh_group_box(sorted(tiers))
            finally:
                self.tree.setUpdatesEnabled(True)

        def _refresh_set_labels(self):
            """Rename set members whose selector curve was just edited (or
            undone): "Injection timing (SOI) — 26–36 °C" follows the curve."""
            p = self.primary
            if p is None:
                return
            ents = list(p.entities) + list(p.variant_entities())
            for e in _catalogue.refresh_notes(ents):
                item = getattr(self, "_item_of", {}).get(id(e))
                if item is not None:
                    label = _english(e)
                    item.setText(0, label)
                    self._leaf_text[id(e)] = _filter_text(e, label)

        def _refresh_group_box(self, present):
            """The group filter lists the groups this file has, in order,
            keeping the current choice when the new file has it too."""
            box = self.tier_box
            cur = box.currentData()
            box.blockSignals(True)
            try:
                box.clear()
                box.addItem("All groups", 0)
                for g in present:
                    box.addItem(tier_name(g), g)
                    box.setItemData(box.count() - 1, _GROUP_HELP.get(g, ""),
                                    QtCore.Qt.ToolTipRole)
                i = box.findData(cur) if cur else 0
                box.setCurrentIndex(i if i >= 0 else 0)
            finally:
                box.blockSignals(False)

        def _on_advanced(self, on):
            try:
                _settings().setValue(ADVANCED_KEY, bool(on))
            except Exception:
                pass
            self._populate()

        def _auto_advanced_if_needed(self):
            """Turn "Advanced" on when EVERY map the file has is in an Advanced
            group, so the tree is never empty while the status bar reports maps
            were found (e.g. a structurally-detected ZF 5HP image, whose tables
            sit in "Other gearbox maps"). Only forces it on, never off, and only
            when nothing would show otherwise."""
            try:
                if self.advanced.isChecked():
                    return
                ents = list(getattr(self.primary, "entities", ()) or ())
                if ents and all(_is_advanced(e) for e in ents):
                    self.advanced.setChecked(True)   # _on_advanced repopulates
            except Exception:
                pass

        def _apply_tree_filters(self):
            want_tier = self.tier_box.currentData()
            adv = self.advanced.isChecked()
            needle = self.filter.text().lower().strip()
            # * and ? are wildcards, within one field of the row's text
            from .search import wildcard_regex as _wildcard
            wild = _wildcard(needle, sep="\0")
            diff_only = (self.diff_only.isChecked()
                         and self.comparison is not None)
            changed = self._changed_set() if diff_only else None
            texts = self._leaf_text

            def shown(e):
                if want_tier and e.tier != want_tier:
                    return False
                # "differs from the comparison" shows every difference,
                # supporting maps included -- a hidden change is a trap
                if (not want_tier and not adv and not diff_only
                        and _is_advanced(e)):
                    return False
                if diff_only and id(e) not in changed:
                    return False
                if wild is not None:
                    return wild.search(texts[id(e)]) is not None
                return not needle or needle in texts[id(e)]

            def walk(node) -> int:
                n = 0
                for i in range(node.childCount()):
                    ch = node.child(i)
                    if ch.childCount():
                        c = walk(ch)
                        hide = c == 0
                        if ch.isHidden() != hide:
                            ch.setHidden(hide)
                        ch.setText(0, "%s (%d)" % (
                            ch.data(0, _BASE_ROLE), c))
                        n += c
                    else:
                        vis = shown(ch.data(0, QtCore.Qt.UserRole))
                        if ch.isHidden() == vis:
                            ch.setHidden(not vis)
                        n += vis
                return n

            sel = self.tree.selectedItems()
            self.tree.setUpdatesEnabled(False)
            try:
                walk(self.tree.invisibleRootItem())
            finally:
                self.tree.setUpdatesEnabled(True)
            if sel and sel[0].isHidden():
                self.tree.clearSelection()
            if needle:
                self.tree.expandAll()
            elif self._last_needle:
                self.tree.collapseAll()
            self._last_needle = needle

        def _current_entity(self) -> Optional[Entity]:
            items = self.tree.selectedItems()
            if not items:
                return None
            return items[0].data(0, QtCore.Qt.UserRole)

        def _on_select(self):
            """A click in the tree: open that table (in the preview tab)."""
            if self._tt_quiet:
                return                  # the tree being moved to match a tab
            e = self._current_entity()
            if e is None:
                return
            self.open_table(e)

        def _on_tree_double(self, item, _col=0):
            e = item.data(0, QtCore.Qt.UserRole) if item is not None else None
            if isinstance(e, Entity) or (e is not None and hasattr(e, "rows")):
                self.open_table(e, pin=True)

        def _on_tree_middle(self, e):
            """A middle-click on a parameter: open it in a new table tab."""
            self.open_table(e, new_tab=True)

        # -- table tabs ---------------------------------------------------
        def _table_at(self, i):
            return self.ttabs.tabData(i) if 0 <= i < self.ttabs.count() else None

        def tables(self):
            """The open table tabs, left to right."""
            return [self.ttabs.tabData(i) for i in range(self.ttabs.count())]

        def _table_index(self, e) -> int:
            for i in range(self.ttabs.count()):
                if self.ttabs.tabData(i).entity is e:
                    return i
            return -1

        def _preview_index(self) -> int:
            for i in range(self.ttabs.count()):
                if not self.ttabs.tabData(i).pinned:
                    return i
            return -1

        def _dress_table_tab(self, i):
            t = self.ttabs.tabData(i)
            e = t.entity
            name = _english(e) or e.name or e.label or "(no symbol)"
            self.ttabs.setTabText(i, _elide(name, 32))
            tip = [name]
            if e.name and e.name != name:
                tip.append(e.name)
            if not t.pinned:
                tip.append("Preview -- the next table you click replaces it. "
                           "Double-click to keep it open; middle-click a "
                           "parameter to open it in a new tab.")
            self.ttabs.setTabToolTip(i, "\n".join(tip))
            self.ttabs.update()

        def open_table(self, e, pin=False, new_tab=False):
            """Show ``e`` in a table tab: its own when it has one, else the
            preview tab (made if there is none). ``pin`` keeps it open;
            ``new_tab`` (a middle-click in the tree) gives it a kept tab of
            its own beside the one showing, leaving the preview alone."""
            if e is None:
                return None
            i = self._table_index(e)
            if i < 0:
                self._save_view()
                i = -1 if new_tab else self._preview_index()
                t = _TableTab(e, pinned=bool(new_tab),
                              heat=self._heat_default)
                self.ttabs.blockSignals(True)
                try:
                    if i >= 0:
                        self.ttabs.setTabData(i, t)
                    else:
                        at = self.ttabs.currentIndex() + 1
                        i = self.ttabs.insertTab(at, "")
                        self.ttabs.setTabData(i, t)
                finally:
                    self.ttabs.blockSignals(False)
                self.ttabs.setVisible(True)
                if self._tt_cur is not None and self._tt_cur not in self.tables():
                    self._tt_cur = None     # the preview it showed was reused
            t = self.ttabs.tabData(i)
            if pin:
                t.pinned = True
            self._dress_table_tab(i)
            if self.ttabs.currentIndex() != i:
                self.ttabs.setCurrentIndex(i)    # -> _on_table_tab
            elif self._tt_cur is not t:
                self._show_table(t)
            return t

        def _on_table_tab(self, i):
            t = self._table_at(i)
            if t is None or t is self._tt_cur:
                return
            self._save_view()
            self._show_table(t)

        def _show_table(self, t):
            """Put table tab ``t`` on the grid, as it was left."""
            self._tt_cur = t
            e = t.entity
            self._tt_quiet = True
            try:
                item = self._find_tree_item(e)
                if item is not None:
                    self.tree.setCurrentItem(item)
                    self.tree.scrollToItem(item)
                else:
                    self.tree.clearSelection()
            finally:
                self._tt_quiet = False
            if self.heat_chk.isChecked() != t.heat:
                self.heat_chk.blockSignals(True)
                self.heat_chk.setChecked(t.heat)
                self.heat_chk.blockSignals(False)
            if self.model._heatmap != t.heat:
                self.model.set_heatmap(t.heat)
            self._zmode_pref = t.zmode or self._zmode_default
            self._render(e)
            if _view6.z_modes(e):
                t.zmode = self._zmode_pref
            self._restore_view(t)

        def _grid_key(self):
            m = self.model
            return [bool(m.transposed), m.rowCount(), m.columnCount()]

        def _save_view(self):
            """Remember where the showing table is scrolled to and what is
            selected in it, for when its tab is come back to."""
            t = self._tt_cur
            if t is None or self.model.entity is not t.entity:
                return
            sm = self.table.selectionModel()
            cur = self.table.currentIndex()
            t.view = {
                "key": self._grid_key(),
                "h": self.table.horizontalScrollBar().value(),
                "v": self.table.verticalScrollBar().value(),
                "sel": [(r.top(), r.left(), r.bottom(), r.right())
                        for r in sm.selection()] if sm is not None else [],
                "cur": (cur.row(), cur.column()) if cur.isValid() else None,
            }

        def _restore_view(self, t):
            v = t.view
            if not v or v.get("key") != self._grid_key():
                return                  # never shown, or the grid has changed
            m = self.model
            sm = self.table.selectionModel()
            sel = QtCore.QItemSelection()
            for top, left, bottom, right in v.get("sel") or ():
                sel.select(m.index(top, left), m.index(bottom, right))
            if v.get("cur"):
                sm.setCurrentIndex(m.index(*v["cur"]),
                                   QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
            sm.select(sel, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.table.doItemsLayout()          # scroll ranges for this grid
            self.table.horizontalScrollBar().setValue(v.get("h", 0))
            self.table.verticalScrollBar().setValue(v.get("v", 0))

        def pin_table(self, i=None):
            """Keep table tab ``i`` (default: the one showing) open."""
            if i is None or i is False:
                i = self.ttabs.currentIndex()
            t = self._table_at(i)
            if t is not None and not t.pinned:
                t.pinned = True
                self._dress_table_tab(i)

        def _pin_shown(self):
            """An edit through the grid keeps its table open."""
            t = self._tt_cur
            if t is not None and self.model.entity is t.entity:
                self.pin_table(self.tables().index(t))

        def close_table(self, i=None):
            """Close table tab ``i`` (default: the one showing)."""
            if i is None or i is False:
                i = self.ttabs.currentIndex()
            t = self._table_at(i)
            if t is None:
                return
            if t is self._tt_cur:
                self._tt_cur = None
            self.ttabs.removeTab(i)         # selects a neighbour, if any
            if self.ttabs.count() == 0:
                self._show_no_table()

        def close_other_tables(self, keep=None):
            keep = self._table_at(self.ttabs.currentIndex()
                                  if keep is None else keep)
            for i in reversed(range(self.ttabs.count())):
                if self.ttabs.tabData(i) is not keep:
                    self.close_table(i)

        def close_tables_right(self, i):
            for j in reversed(range(i + 1, self.ttabs.count())):
                self.close_table(j)

        def close_all_tables(self):
            for i in reversed(range(self.ttabs.count())):
                self.close_table(i)

        def cycle_table(self, step):
            n = self.ttabs.count()
            if n > 1:
                self.ttabs.setCurrentIndex(
                    (self.ttabs.currentIndex() + step) % n)

        def _show_no_table(self):
            self._tt_cur = None
            self.ttabs.setVisible(False)
            self.model.set_entity(None, None)
            self._show_comparison(None, None)
            self.title.setText("Select a parameter")
            self.desc.setText("")
            self._tt_quiet = True
            try:
                self.tree.clearSelection()
            finally:
                self._tt_quiet = False

        def _drop_tables(self):
            """Forget every table tab (the file under them is going), and
            return what they were, for :meth:`restore_tables`."""
            state = self.tables_state()
            self._tt_cur = None
            self.ttabs.blockSignals(True)
            try:
                while self.ttabs.count():
                    self.ttabs.removeTab(0)
            finally:
                self.ttabs.blockSignals(False)
            self.ttabs.setVisible(False)
            return state

        def tables_state(self):
            """The open tables, as the tab history stores them.

            Referenced through ``session.entity_ref``, which falls back to the
            entity's position in its structurally detected family when it has no
            symbol. This used to skip every unnamed entity -- on a ZF 6HP image
            that is all 82 shift schedules and all 12 TCC maps, the only tables
            anyone keeps open, so reopening a file never brought them back.

            The active index is tracked while building the list rather than
            looked up by name afterwards: two tabs can hold entities with the
            same name, and an unnamed one could not be found at all.
            """
            from .session import entity_ref
            out = []
            active = None
            cur = self._tt_cur
            for t in self.tables():
                ref = entity_ref(t.entity)
                if ref is None:
                    continue            # nothing to find it by again
                if t is cur:
                    active = len(out)
                ref.update({"pinned": t.pinned, "heat": t.heat,
                            "zmode": t.zmode})
                out.append(ref)
            return {"tables": out, "table_active": active}

        def _find_entity(self, name, addr=None, scan=None):
            from .session import find_by_ref
            return find_by_ref(self.primary,
                               {"name": name, "addr": addr, "scan": scan})

        def restore_tables(self, tables, active=None):
            """Reopen table tabs from :meth:`tables_state` -- the ones this
            file has -- and show the one that was active."""
            made = []
            self.ttabs.blockSignals(True)
            try:
                for d in tables or ():
                    if not isinstance(d, dict):
                        continue
                    e = self._find_entity(d.get("name"), d.get("addr"),
                                          d.get("scan"))
                    if e is None or self._table_index(e) >= 0:
                        made.append(None)
                        continue
                    t = _TableTab(e, pinned=bool(d.get("pinned", True)),
                                  heat=bool(d.get("heat", False)),
                                  zmode=d.get("zmode") or None)
                    if not t.pinned and self._preview_index() >= 0:
                        t.pinned = True     # only one preview at a time
                    i = self.ttabs.addTab("")
                    self.ttabs.setTabData(i, t)
                    self._dress_table_tab(i)
                    made.append(t)
            finally:
                self.ttabs.blockSignals(False)
            live = [t for t in made if t is not None]
            if not live:
                return []
            self.ttabs.setVisible(True)
            pick = made[active] if isinstance(active, int) \
                and 0 <= active < len(made) and made[active] is not None \
                else live[-1]
            self._tt_cur = None
            i = self.tables().index(pick)
            if self.ttabs.currentIndex() != i:
                self.ttabs.setCurrentIndex(i)
            else:
                self._show_table(pick)
            return live

        def _heat_toggled(self, on):
            self.model.set_heatmap(on)
            self._heat_default = bool(on)
            if self._tt_cur is not None:
                self._tt_cur.heat = bool(on)

        def _table_tab_menu(self, pos):
            i = self.ttabs.tabAt(pos)
            t = self._table_at(i)
            if t is None:
                return
            menu = QtWidgets.QMenu(self)
            if not t.pinned:
                menu.addAction("Keep open", lambda: self.pin_table(i))
            menu.addAction("Close\tCtrl+F4", lambda: self.close_table(i))
            other = menu.addAction("Close other tables",
                                   lambda: self.close_other_tables(i))
            other.setEnabled(self.ttabs.count() > 1)
            right = menu.addAction("Close tables to the right",
                                   lambda: self.close_tables_right(i))
            right.setEnabled(i < self.ttabs.count() - 1)
            menu.addAction("Close all tables", self.close_all_tables)
            if t.entity.name:
                menu.addSeparator()
                menu.addAction(
                    "Copy symbol name", lambda: QtWidgets.QApplication
                    .clipboard().setText(t.entity.name))
            menu.exec(self.ttabs.mapToGlobal(pos))

        def _render(self, e):
            """Draw ``e`` on the grid with its comparison, title and notes."""
            cmp_e = None
            if self.comparison is not None:
                cmp_e = self.primary.counterpart(self.comparison, e)
                # Say so when there is nothing to show. The comparison block is
                # simply absent from the grid when it has no values, and an
                # absent block is indistinguishable from a comparison that was
                # never opened -- which is exactly how a pairing bug in the
                # shift and TCC families went unexplained: the report was "the
                # comparison table is not visible", with nothing on screen to
                # say why. One line in the log beats a blank area.
                if cmp_e is None:
                    self._note_no_counterpart(e)
            self.model.set_entity(e, cmp_e)
            self._sync_transpose(e)
            self._fit_columns()
            self._show_comparison(e, cmp_e)
            self._set_title(e)
            src = {"anchored": "exact match (fingerprint)",
                   "struct": "structure-verified",
                   "verified": "value-verified",
                   "placed": "placed by the ECU's pointer table",
                   "bracketed": "bracketed (layout unchanged around it)",
                   "projected": "PROJECTED - offset model only; check before writing",
                   "structure": "found by structure (no definition places it) - check before writing"
                   }.get(e.source, e.source)
            if getattr(e, "note", ""):
                src += ": " + e.note
            # main description + any axis descriptions, laid out for the panel
            main, axes = _parse_desc(e)
            lines = []
            if main:
                lines.append(main)
            if axes.get("Z"):
                lines.append("Value: %s" % axes["Z"])
            for tag, axis, lbl in (("X", getattr(e, "x", None), "X axis"),
                                   ("Y", getattr(e, "y", None), "Y axis")):
                d = axes.get(tag, "")
                if d or (axis is not None and getattr(axis, "count", 0)):
                    u = (axis.units or "").strip() if axis is not None else ""
                    n = getattr(axis, "count", 0) if axis is not None else 0
                    bits = []
                    if n:
                        bits.append("%d pts" % n)
                    if u:
                        bits.append(u)
                    suffix = " (%s)" % ", ".join(bits) if bits else ""
                    lines.append("%s: %s%s" % (lbl, d or "-", suffix))
            if not lines:
                lines.append("(no description)")
            banner = ""
            ov = self.model.overlay
            if ov is not None:
                tag = {"direct": "comparison: direct cell diff",
                       "axis-mismatch": "comparison: same size, "
                       "AXES DIFFER - cells are at different operating points",
                       "resampled": "comparison: " + ov.note}.get(
                           ov.mode, ov.mode)
                banner = "\n" + tag
            lines.append("Group: %s | %s" % (tier_name(e.tier), src))
            self.desc.setText("\n".join(lines) + banner)
            # (no second resizeColumnsToContents: _fit_columns above sized the
            # columns from sampled rows; a full one here read every cell again
            # and was half the time a click took)

        # -- file ops ---------------------------------------------------
        def _set_title(self, e):
            """Name, text, kind and shape AS SHOWN -- the same for a 1-D table
            whichever way its definition stores it, and following Swap X and
            Y axes."""
            text = _ascii_of(e)
            shape = _shape_text(e, self.model.transposed
                                if self.model.entity is e else None)
            # When a comparison is open but has nothing to line up against this
            # table, say so in the title. The comparison block is simply absent
            # from the grid, and an absent block looks identical to no comparison
            # at all -- which is how a pairing bug in the shift and TCC families
            # went unexplained: "the comparison table is not visible", with
            # nothing on screen to say why.
            missing = ""
            if self.comparison is not None and self.model.entity is e \
                    and self.model.compare is None:
                missing = "   - no match in %s" % self.comparison.binfile.name

            self.title.setText("%s%s   [%s%s]   %s%s" % (
                e.name or e.label or "(no symbol)",
                '   "%s"' % text if text else "", _kind_text(e),
                "  " + shape if shape else "", e.units, missing))

        def compare_with_version(self, version_id, quiet=False) -> bool:
            """Make one of the primary's own saved versions (its version
            history) this tab's comparison -- "what did this file look like
            before that change", without keeping numbered copies around."""
            if self.primary is None:
                return False
            if self.project_ref is not None:
                return _gp.compare_with_checkpoint(self, version_id, quiet=quiet)
            from . import patch as _patch
            label = self.primary.binfile.name
            try:
                comp = self._load_session(
                    label, title="Loading saved version",
                    loader=lambda report: _patch.session_at_version(
                        self.primary, version_id, progress=report))
            except Exception as ex:
                if not quiet:
                    QtWidgets.QMessageBox.critical(
                        self, "Compare with saved version",
                        "That version could not be rebuilt: %s" % ex)
                return False
            self.set_comparison(comp)
            if not quiet:
                v = comp.version_of
                self.statusBar().showMessage(
                    "Comparing against the saved version \"%s\" (%s)."
                    % (v["label"], v["ts"].replace("T", " ")), 8000)
            return True

        def _load_session(self, path, title="Loading", parent=None, loader=None):
            """Open a binary behind a determinate progress dialog.

            Identifying the family scores every knowledge base and the matcher runs
            eight passes over a 2 MB image, so a load takes seconds, not
            milliseconds. TunerSession reports each step; this turns those into a
            moving bar and pumps the event loop, so the window stays responsive
            instead of going grey behind a message that never changes. Cancelling is
            deliberately not offered -- the passes share state and there is no safe
            point to stop at.
            """
            total = _progress.STEPS["edc16"]
            dlg = QtWidgets.QProgressDialog("Opening %s..." % _os.path.basename(path),
                                            None, 0, total, parent or self)
            dlg.setWindowTitle(title)
            dlg.setWindowModality(QtCore.Qt.WindowModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.setAutoClose(False)
            dlg.setAutoReset(False)
            dlg.show()

            def report(label, done, total_):
                if dlg.maximum() != total_:
                    dlg.setMaximum(total_)
                dlg.setValue(done)
                dlg.setLabelText("%s\n%s" % (_os.path.basename(path), label))
                QtWidgets.QApplication.processEvents()

            report("Starting...", 0, total)
            try:
                if loader is not None:
                    return loader(report)
                return TunerSession(path, progress=report)
            finally:
                dlg.close()

        def _load_primary(self, path) -> bool:
            """Open ``path`` as this tab's primary. True when it loaded.

            What it replaces goes into the closed-tab history, so opening over
            a file by mistake is one menu pick away from undone."""
            if not self.confirm_close(
                    "Save them before opening another file in this tab?"):
                return False
            gone = self.win.closed_entry(self)
            ok = self._load_primary_into(path)
            if ok and gone is not None:
                self.win.push_closed(gone)
            return ok

        def _load_primary_into(self, path) -> bool:
            if _gp.parse_ref(path) is not None:
                return _gp.load_ref(self, path)     # a tab of an .ovt project
            if _gp.is_project(path):
                return _gp.open_project(self.win, path)
            kind = _gp.definition_kind(path)
            if kind == "ols":                   # a WinOLS project: binary + maps
                return _gp.open_ols(self.win, path, ws=self)
            if kind:                            # a definition holds no binary
                return _gp.open_definition(self.win, path, kind)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(8)
            except OSError as ex:
                QtWidgets.QMessageBox.critical(self, "Error", str(ex))
                return False
            from .flash import backup15
            if backup15.is_backup(head):
                # import-only: becomes a set (flash + EEPROM) in a project
                from . import gui_files as _gf
                return _gf.import_files(self.win, [path])
            try:
                loaded = self._load_session(path)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Error", str(ex))
                from .recent import remove_recent
                remove_recent(path)
                self._rebuild_recent()
                return False
            return self.adopt_primary(loaded, path)

        def adopt_primary(self, loaded, path) -> bool:
            """Make an already-loaded session this tab's primary. The tables
            that were open stay open where the new file has them too."""
            _gp.leaving(self)               # the old one's definition snapshot
            self.shutdown()                 # tool windows were the old file's
            open_tables = self._drop_tables()
            dropped = self._carry_comparison(loaded)
            self.primary = loaded
            self._leave_project(self.__dict__.pop("_pending_project_ref", None))
            # a saved version opened read-only (gui_viewonly), or not
            self.view_only = self.__dict__.pop("_pending_view_only", None)
            self.model.readonly = bool(self.view_only)
            try:
                loaded.correct_program_area = program_area_pref()
            except Exception:
                pass
            self._untitled = False
            self._backup_path = None
            self._eeprom = None
            self._eeprom_dirty = False
            self.model.set_entity(None, None)
            self.model.session = self.primary
            self._show_comparison(None, None)
            self.title.setText("Select a parameter")
            self.desc.setText("")
            if self.project_ref is None and not self.view_only:
                from .recent import add_recent
                add_recent(path)
                self._rebuild_recent()
            self._refresh_enabled()
            self._auto_advanced_if_needed()
            self._populate()
            self.restore_tables(open_tables["tables"],
                                open_tables["table_active"])
            self._status()
            self._say_dropped(dropped)
            cont = getattr(loaded, "_container", None)
            if (cont is not None and loaded.defn_state not in ("used", "none")
                    and loaded.platform in ("edc16", "edc15", "zf6hp")):
                self.statusBar().showMessage(
                    "Opened the slow way (%s). Save to store the definition "
                    "and open faster next time." % loaded.defn_state, 10000)
            if getattr(loaded, "support", "full") not in ("full", "definition"):
                self._explain_limited_support(loaded)
            else:
                self.show_file_info(loaded=True)
            if getattr(self.primary, "sgo_warning", None):
                QtWidgets.QMessageBox.warning(
                    self, "Partially-decoded .sgo container", self.primary.sgo_warning)
            if getattr(self.primary, "load_note", None):
                QtWidgets.QMessageBox.warning(
                    self, "%s: notes on this file"
                    % platform_badge(self.primary.platform)[1],
                    self.primary.load_note)
            if getattr(self.primary, "frf_warning", None):
                self.statusBar().showMessage(self.primary.frf_warning, 12000)
            return True

        def _explain_limited_support(self, loaded):
            """A binary whose module is not installed (or that nothing
            supports): say what it is and what would support it, then open it
            in the hex editor -- the universal tools all still work."""
            if getattr(self, "_quiet", False):
                return                      # reopening tabs: no popup each
            reason = (getattr(loaded, "unsupported_reason", None)
                      or "This binary has no parameter definition.")
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Information)
            box.setWindowTitle("Limited support - %s" % loaded.binfile.name)
            box.setText(reason)
            avail = ("Checksum verify/correct is available. "
                     if getattr(loaded, "support", "") == "checksum" else "")
            box.setInformativeText(
                avail + "The file opens in the hex editor; compare, patches, "
                "version history and the flasher work as usual.")
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            mid = _platforms.module_for(loaded.platform)
            install = github = None
            if mid and not _registry.can(loaded.platform, "definitions") \
                    and _registry.spec(mid) is None:
                from . import gui_getmodule as _gm  # published on GitHub?
                github, avail = _gm.offer_button(box, mid)
                install = box.addButton("Install module...",
                                        QtWidgets.QMessageBox.ButtonRole.ActionRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is not None and clicked in (install, github):
                from .gui_modules import install_from_file
                got = (_gm.install(self, mid, avail) if clicked is github
                       else install_from_file(self))
                if got is not None \
                        and _registry.can(loaded.platform, "checksum") \
                        and getattr(loaded, "path", None):
                    # the module is in: open the binary again, with it -- a
                    # project tab from its project (once this load is done;
                    # loaded.path is only its working copy)
                    if (getattr(self, "project_ref", None)
                            or getattr(self, "_pending_project_ref", None)):
                        QtCore.QTimer.singleShot(
                            0, lambda: self.project_ref is not None
                            and _gp.reload_tab(self))
                    else:
                        self._load_primary_into(loaded.path)
                    return
            self.open_hex()

        def _carry_comparison(self, new_primary):
            """A new primary keeps the tab's comparison -- comparing a second
            file against the same reference is the usual reason to open one
            -- unless it is another platform, which cannot pair with it (see
            pair_problem). Returns the comparison that had to go, or None."""
            comp = self.comparison
            self._changed_cache = None
            if comp is not None and comp.platform == new_primary.platform:
                return None                 # kept, live link and all
            self.comparison = None
            self.cmp_link = None
            self.cmp_version = None
            self.cmp_frozen = False
            self._seen_cmp_rev = None
            self.diff_only.blockSignals(True)
            self.diff_only.setChecked(False)
            self.diff_only.setEnabled(False)
            self.diff_only.blockSignals(False)
            return comp

        def _say_dropped(self, comp):
            if comp is None or self.primary is None:
                return
            _pb, pname, _pc = platform_badge(self.primary.platform)
            _cb, cname, _cc = platform_badge(comp.platform)
            self.statusBar().showMessage(
                "Comparison %s closed: it is %s, the new primary is %s."
                % (comp.binfile.name, cname, pname), 8000)

        def _load_comparison(self, path) -> bool:
            if self.primary is None:
                QtWidgets.QMessageBox.information(
                    self, "Open comparison",
                    "Open a primary binary first, then a comparison.")
                return False
            try:
                comp = self._load_session(path, title="Loading comparison")
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Error", str(ex))
                from .recent import remove_recent_comparison
                remove_recent_comparison(path)
                self._rebuild_recent_comparisons()
                return False
            problem = self.pair_problem(comp)
            if problem:
                # It is loaded already, and loading is the slow part: offer
                # it a tab of its own rather than throwing the work away.
                SB = QtWidgets.QMessageBox.StandardButton
                box = QtWidgets.QMessageBox(
                    QtWidgets.QMessageBox.Icon.Information,
                    "Different platform", problem, SB.Cancel, self)
                own = box.addButton("Import it into a new project",
                                    QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                box.setDefaultButton(own)
                box.exec()
                if box.clickedButton() is own:
                    from . import gui_files as _gf
                    if _gp.parse_ref(path) is None and _os.path.isfile(path):
                        _gf.import_files(self.win, [path], target="new")
                    else:
                        self.win.adopt_session(comp, path)
                return False
            from .recent import add_recent_comparison
            add_recent_comparison(path)     # (the picker records its folder)
            self._rebuild_recent_comparisons()
            self.set_comparison(comp)
            self.show_file_info(self.comparison, loaded=True)
            return True

        def link_comparison(self, other) -> bool:
            """Compare against another tab's primary, following its edits."""
            if other is self or other.primary is None or self.primary is None:
                return False
            problem = self.pair_problem(other.primary)
            if problem:
                QtWidgets.QMessageBox.information(
                    self, "Different platform", problem)
                return False
            self.set_comparison(other.primary, link=other)
            return True

        def close_comparison(self):
            if self.comparison is None:
                return
            self.set_comparison(None)

        def _leave_project(self, new_ref=None):
            """This tab now shows ``new_ref`` (a project branch) or a plain
            file: the project it showed before, if any, loses a tab."""
            old = self.project_ref
            self.project_ref = new_ref
            if old is not None and (new_ref is None or new_ref.doc is not old.doc):
                _gp.released(self.win, old.doc)

        def close_primary(self):
            if self.primary is None:
                return
            if self.primary.modified:
                if QtWidgets.QMessageBox.question(
                        self, "Unsaved changes",
                        "The current binary has unsaved changes. Close it and "
                        "discard them?") \
                        != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            _gp.leaving(self)               # its definition snapshot stays
            self.shutdown()
            self._drop_tables()
            self.primary = None
            self._leave_project()
            self.comparison = None
            self.cmp_link = None
            self.cmp_version = None
            self.cmp_frozen = False
            self._untitled = False
            self._backup_path = None
            self._eeprom = None
            self._eeprom_dirty = False
            self._changed_cache = None
            old = getattr(self, "_diff_dlg", None)
            if old is not None:
                old.close()
                self._diff_dlg = None
            self.model.set_entity(None, None)
            self.model.session = None
            self._show_comparison(None, None)
            self.title.setText("Select a parameter")
            self.desc.setText("")
            self.diff_only.setChecked(False)
            self.diff_only.setEnabled(False)
            self._populate()
            self._refresh_enabled()
            self._status()

        def show_differences(self):
            if self.comparison is None:
                return
            changed = self._changed_set()
            rows = []
            for e in (list(self.primary.entities)
                      + list(self.primary.variant_entities())):
                if id(e) not in changed:
                    continue
                cp = self.primary.counterpart(self.comparison, e)
                rows.append((e, cp))
            if not rows:
                from . import patch as _patch
                has_raw = bool(_patch.diff_outside_calibration(
                    self.primary, self.comparison))
                QtWidgets.QMessageBox.information(
                    self, "Differences",
                    "No parameter differs between the two binaries."
                    + ("\n\nThe program/boot area (or the MPC/internal "
                       "flash or EEPROM, where the set holds them) does, "
                       "though -- open the dialog and tick Show: "
                       "Program/boot, MPC or EEPROM to see it."
                       if has_raw else ""))
                if not has_raw:
                    return
            old = getattr(self, "_diff_dlg", None)
            if old is not None:
                old.close()
            self._diff_dlg = DiffListDialog(self.primary, self.comparison, rows, self)
            self._diff_dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            self._diff_dlg.show()

        def save(self, tab_only=False) -> bool:
            """Save. A project tab saves its project (``tab_only``: just this
            tab's edits); any other tab becomes a new ``.ovt`` project on its
            first save. Files in other formats are made with File > Export.
            True once it is written; False when cancelled or it failed."""
            if self.primary is None:
                return False
            if getattr(self, "view_only", None):
                from . import gui_viewonly as _vo
                return _vo.refuse_save(self)
            if self.project_ref is not None:
                return _gp.save(self, tab_only=tab_only)
            return _gp.save_as_project(self)

        def _sync_eeprom_to_primary(self):
            """A held EEPROM (a full backup's, or one edited in the EEPROM
            editor) goes into the container being saved, so a .edc15 written
            from a backup carries its EEPROM and an edit is not lost."""
            ee = getattr(self, "_eeprom", None)
            if ee is not None and self.primary is not None:
                self.primary._region_edits[self.primary.EEPROM_REGION] = \
                    bytearray(ee)
                self._eeprom_dirty = False

        def _save_failed(self, path, ex):
            """Tell the user a save did not happen, and log why."""
            import traceback
            try:
                self.primary.log("save to %s failed: %s\n%s" % (
                    path, ex, traceback.format_exc()))
            except Exception:
                pass
            QtWidgets.QMessageBox.critical(
                self, "Not saved",
                "%s could not be saved:\n\n%s: %s\n\nYour changes are "
                "still open and unsaved." % (path, type(ex).__name__, ex))

        def _after_save(self):
            # The tab first: the file is written by now, and nothing below
            # may leave the "*" standing. (It did -- only the EDC16 report
            # has a ``slots`` list, so saving a ZF 6HP or HVAC image raised
            # here before the tab was ever redrawn.)
            self._status()
            rep = self.primary.last_checksum
            name = self.primary.binfile.name
            if rep is None:
                # an MSA15 / EDC15V or first-EDC15P image whose checksum
                # routine is not one the app reads
                v3 = any(getattr(d, "v3", False) or str(getattr(d, "sig", "")).startswith("P2")
                         for d in getattr(self.primary, "datasets", None) or ())
                self.statusBar().showMessage(
                    "Saved %s%s" % (name, " - checksums NOT corrected (this "
                                    "software's checksum routine is not one the "
                                    "app reads)" if v3 else ""), 10000 if v3 else 6000)
                return
            slots = getattr(rep, "slots", None) or []
            names = ", ".join(str(getattr(sl, "name", sl)) for sl in slots)
            self.statusBar().showMessage(
                "Saved %s - %s checksums corrected%s, image ready to flash"
                % (name, self.primary.platform.upper(),
                   " (%s)" % names if names else ""), 6000)

        def save_tab_only(self) -> bool:
            """File > Save this branch only (a project tab)."""
            return self.save(tab_only=True)

        def save_as_project(self) -> bool:
            """File > Save as project: this tab's file becomes an .ovt."""
            return _gp.save_as_project(self)

        def save_as(self):
            """Save As: the project under a new name -- or, for a tab that is
            not a project yet, as a new project."""
            if getattr(self, "view_only", None):
                from . import gui_viewonly as _vo
                return _vo.refuse_save(self)
            if self.project_ref is not None:
                return _gp.save_as(self)
            return _gp.save_as_project(self)

        def verify_checksums(self):
            rep = self.primary.checksum_status()
            if rep is None:
                if self.primary.checksum_provider() is None:
                    # nothing installed can check this platform at all
                    ident = getattr(self.primary, "binary_identity", None)
                    QtWidgets.QMessageBox.information(
                        self, "Checksums",
                        "%s.\n\n%s" % (
                            ident.describe() if ident is not None
                            else self.primary.binfile.name,
                            _registry.missing_text(self.primary.platform,
                                                   "checksum support")))
                    return
                QtWidgets.QMessageBox.information(
                    self, "Checksums", _registry.checksum_unsupported_text(
                        self.primary.platform, self.primary.checksum_provider(),
                        self.primary.binfile.data))
                return
            if getattr(rep, "unreadable", ""):
                # Not "wrong" -- unknown. Offering to correct would be offering
                # to write bytes from a model that could not be derived.
                QtWidgets.QMessageBox.warning(
                    self, "Checksums cannot be verified", rep.describe())
                return
            # an EDC16 file's EEPROM region (.edc16 / .dim) is reported too
            ee = self.primary.eeprom_checksum_status()
            ee_txt = ("\n\n" + ee.describe()) if ee is not None else ""
            ee_bad = ee is not None and ee.identified and not ee.ok
            if rep.ok and not ee_bad:
                QtWidgets.QMessageBox.information(
                    self, "Checksums", "All %s checksums are valid.\n\n"
                    % self.primary.platform.upper() + rep.describe() + ee_txt)
                return
            msg = (rep.describe() + ee_txt
                   + ("\n\nCorrect them now? (Save also corrects "
                      "automatically.)" if ee is None else
                      "\n\nCorrect them now? (Save also corrects the flash "
                      "checksums automatically, and the EEPROM's after an "
                      "edit to it.)"))
            if QtWidgets.QMessageBox.question(
                    self, "Checksums need correction", msg) \
                    == QtWidgets.QMessageBox.StandardButton.Yes:
                try:
                    self.primary.correct_checksums()
                    if ee_bad:
                        self.primary.correct_eeprom_checksums()
                except ValueError as exc:
                    # A refusal, not a bug: the EDC15 path declines to write
                    # checksums it cannot derive rather than write a guess.
                    QtWidgets.QMessageBox.warning(
                        self, "Checksums not corrected", str(exc))
                    return
                self._status()
                QtWidgets.QMessageBox.information(
                    self, "Checksums", "Checksums corrected in memory. "
                    "Save to write the image.")

        def export_xdf(self):
            from .xdf_export import export_xdf
            from .recent import get_last_dir, set_last_dir
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export XDF", get_last_dir("export"), filter="XDF files (*.xdf)")
            if path:
                set_last_dir(path, "export")
                xml = export_xdf(
                    self.primary.entities + self.primary.variant_entities(),
                    branding.tagged(self.primary.binfile.name),
                    self.primary.binfile.name,
                    image_size=len(self.primary.binfile.data),
                    platform=self.primary.platform)
                with open(path, "w", encoding="ascii",
                          errors="replace") as fh:
                    fh.write(xml)

        # -- edit -------------------------------------------------------
        def undo(self):
            self.primary.undo()
            self.model.refresh()
            self._status()

        def redo(self):
            self.primary.redo()
            self.model.refresh()
            self._status()

        def _selected_indexes(self):
            sm = self.table.selectionModel()
            return sm.selectedIndexes() if sm else []

        def _apply_op(self):
            if self.model.entity is None:
                return
            try:
                value = float(self.op_val.text().strip())
            except ValueError:
                self.statusBar().showMessage("Enter a number to apply.", 4000)
                return
            op = self.op_box.currentData()
            idxs = self._selected_indexes()
            if not idxs:              # nothing selected -> whole table
                idxs = [self.model.index(r, c)
                        for r in range(self.model.rowCount())
                        for c in range(self.model.columnCount())]
            n = self.model.apply_op(idxs, op, value)
            if n:
                self.statusBar().showMessage(
                    "%s applied to %d cell(s)." % (
                        self.op_box.currentText(), n), 4000)
                self._status()
            else:
                self.statusBar().showMessage(
                    "No editable cells in selection.", 4000)

        def _interp(self, mode):
            if self.model.entity is None:
                return
            idxs = self._selected_indexes()
            if len(idxs) < 2:
                self.statusBar().showMessage(
                    "Select a range of cells to interpolate.", 4000)
                return
            n = self.model.interpolate(idxs, mode)
            self.statusBar().showMessage(
                "Interpolated %d cell(s)." % n if n else
                "Nothing to interpolate in that selection.", 4000)
            if n:
                self._status()

        def _smooth_sel(self):
            if self.model.entity is None:
                return
            idxs = self._selected_indexes()
            if len(idxs) < 2:
                self.statusBar().showMessage(
                    "Select the cells to smooth.", 4000)
                return
            n = self.model.smooth(idxs)
            self.statusBar().showMessage(
                "Smoothed %d cell(s)." % n if n else "No change.", 4000)
            if n:
                self._status()

        def _copy_cells(self):
            idxs = self._selected_indexes()
            if not idxs:
                return
            text = self.model.copy_tsv(idxs)
            QtWidgets.QApplication.clipboard().setText(text)
            self.statusBar().showMessage("Copied selection.", 3000)

        def _paste_cells(self):
            if self.model.entity is None:
                return
            idxs = self._selected_indexes()
            anchor = min(idxs, key=lambda i: (i.row(), i.column())) \
                if idxs else self.table.currentIndex()
            if anchor is None or not anchor.isValid():
                anchor = self.model.index(0, 0)
            text = QtWidgets.QApplication.clipboard().text()
            n = self.model.paste_tsv(anchor, text)
            if n:
                self.statusBar().showMessage("Pasted %d value(s)." % n, 3000)
                self._status()

        def scale(self):
            # kept for the Edit menu / Ctrl-key; multiplies the CURRENT
            # SELECTION (falling back to the whole table if nothing selected)
            # of the table that is SHOWING -- like the edit bar's Multiply,
            # whatever the tree has selected (a filter may hide that row).
            if self.model.entity is None:
                return
            factor, ok = QtWidgets.QInputDialog.getDouble(
                self, "Scale selection", "Multiply selected cells by:", 1.05,
                -1e6, 1e6, 4)
            if not ok:
                return
            idxs = self._selected_indexes()
            if not idxs:
                idxs = [self.model.index(r, c)
                        for r in range(self.model.rowCount())
                        for c in range(self.model.columnCount())]
            n = self.model.apply_op(idxs, "mul", factor)
            if not n:
                self.statusBar().showMessage(
                    "No editable cells in selection.", 4000)
                return
            self.statusBar().showMessage("Scaled %d cell(s) ×%g." % (n, factor),
                                         4000)
            self._status()

        def open_dtc(self):
            """The fault-code editor for whichever platform is loaded.

            Two windows: the engine one (an EDC16 fault has its own DSM path,
            class and lamps; an EDC15 sub-fault its defect time) and the
            gearbox one, which shows whatever model the gearbox module gives
            (a ZF 6HP code switched through its class's priority byte, a ZF
            8HP fault through its own diagnosis-on bit)."""
            if self.primary is None:
                return
            if getattr(self.primary, "platform", "") in ("zf6hp", "zf8hp"):
                from .gui_tcu_dtc import open_for as _tcu_dtc
                self.open_tool("dtc", lambda: _tcu_dtc(
                    self.primary, self, compare=self.comparison))
            else:
                from .gui_dtc import DtcDialog as _DtcDialog
                self.open_tool("dtc", lambda: _DtcDialog(
                    self.primary, self, compare=self.comparison))

        def open_patch_builder(self, swap=None):
            """Edit > Generate patch... A patch carries what changes from
            one binary to the other: when the comparison is one of this
            tab's own versions (a project tab starts out compared with the
            original as read) that is from the comparison to this tab --
            the tab's changes -- else from this tab to the comparison.
            **Swap direction** turns it round."""
            if self.comparison is None:
                return
            if swap is None:
                swap = getattr(self.comparison, "version_of", None) is not None
            a, b = ((self.comparison, self.primary) if swap
                    else (self.primary, self.comparison))
            dlg = PatchBuilderDialog(
                a, b, self, on_swap=lambda: self.open_patch_builder(not swap))
            dlg.exec()

        def open_patch_apply(self):
            if self.primary is None:
                return
            import os as _os
            from . import patch as _patch
            from .recent import get_last_dir, set_last_dir
            doc = self.project_ref.doc if self.project_ref is not None else None
            if doc is not None and doc.project.patches:
                # the project's own patches first, a file from disk after
                pats = _gp.newest_first(doc.project.patches)
                names = [a.filename for a in pats]
                pick, ok = QtWidgets.QInputDialog.getItem(
                    self, "Apply patch", "Patch kept in %s:" % doc.title,
                    names + ["From a file..."], 0, False)
                if not ok:
                    return
                if pick != "From a file...":
                    pid = pats[names.index(pick)].id
                    _gp.apply_project_patch(self, doc, pid)
                    return
            path, _f = QtWidgets.QFileDialog.getOpenFileName(
                self, "Apply patch", get_last_dir("patch"),
                "Patch files (*.patch);;All files (*)")
            if not path:
                return
            set_last_dir(path, "patch")
            try:
                pt = _patch.Patch.load(path)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(
                    self, "Apply Patch", "Could not read this patch file:\n%s" % exc)
                return
            dlg = PatchApplyDialog(self.primary, pt, _os.path.basename(path), self)
            dlg.exec()
            self.model.refresh()
            self._status()

        def open_patch_remove(self):
            if self.primary is None:
                return
            dlg = RemovePatchDialog(self.primary, self)
            dlg.exec()
            self.model.refresh()
            self._status()

        def open_version_history(self):
            if self.primary is None:
                return
            on_merge = None
            if self.project_ref is not None:
                ref = self.project_ref
                on_merge = (lambda ids, name, _r=ref: _gp.merge_versions(
                    self.win, _r.doc, _r.sid, ids, name))
            dlg = VersionHistoryDialog(self.primary, self,
                                       on_compare=self.compare_with_version,
                                       on_merge=on_merge)
            if self.project_ref is not None:
                dlg.hint.setText(
                    "Double-click a name (or press F2) to rename a version. "
                    "Select adjacent versions to merge them into one. Going "
                    "back to one adds a new version with its contents.")
            dlg.exec()
            if self.project_ref is not None:
                _gp.after_history_dialog(self)
            self.model.refresh()
            self._status()

        def open_history_patch(self):
            if self.primary is None:
                return
            dlg = HistoryPatchDialog(self.primary, self)
            dlg.exec()

        def open_msblock(self):
            if self.primary is None:
                return
            self.open_tool("msblock", lambda: MsBlockDialog(
                self.primary, self, compare=self.comparison))

        def open_shift_editor(self):
            """Tools > ZF 6HP > Shift map editor... (gui_gearbox_edit)."""
            from .gui_gearbox_edit import open_editor
            return open_editor(self)

        def open_tcc_editor(self):
            """Tools > ZF 6HP > Lock-up (TCC) editor... (gui_gearbox_edit)."""
            from .gui_gearbox_edit import open_editor
            return open_editor(self, "tcc")

        def open_gears(self):
            g = GearRatios(self.primary)
            if not g.available:
                QtWidgets.QMessageBox.information(
                    self, "Gear ratios",
                    "This binary has no recognised drivetrain-ratio "
                    "parameters.")
                return
            self.open_tool("gears", lambda: GearRatioDialog(
                self.primary, self, compare=self.comparison))

        def open_vss(self):
            if getattr(self.primary, "platform", "") == "vdo":
                from .vdo import speed as _vs
                if _vs.k_value(self.primary) is None:
                    QtWidgets.QMessageBox.information(
                        self, "Speed & distance",
                        "This cluster definition has no K-value.")
                    return
            elif getattr(self.primary, "is_edc15", False):
                from .edc15.vss15 import speed_settings
                if not speed_settings(self.primary).available:
                    QtWidgets.QMessageBox.information(
                        self, "VSS pulse settings",
                        "No EDC15 FGG speed blocks were found in this binary.")
                    return
            elif not vss_pulse_settings(self.primary):
                QtWidgets.QMessageBox.information(
                    self, "VSS pulse settings",
                    "No vehicle-speed (VSSCD_ImpPerDist_C) calibration was "
                    "found in this binary.")
                return
            self.open_tool("vss", lambda: VssDialog(
                self.primary, self, compare=self.comparison))

        def open_sweep(self):
            if getattr(self.primary, "platform", "") != "vdo":
                QtWidgets.QMessageBox.information(
                    self, "Cluster needle sweep",
                    "Needle sweep patches are for VDO instrument-cluster "
                    "EEPROMs (.vdo).")
                return
            if not self.primary.vdo_definition.patch_module:
                QtWidgets.QMessageBox.information(
                    self, "Cluster needle sweep",
                    "No patch-module location is known yet for the %s "
                    "cluster family, so sweep patches cannot be placed."
                    % self.primary.vdo_definition.family)
                return
            self.open_tool("sweep", lambda: SweepDialog(self.primary, self))

        # -- ZF 6HP Z display mode -------------------------------------
        def _sync_zmode(self):
            """Show or hide the selector for whatever map is now selected."""
            modes = _view6.z_modes(self.model.entity)
            for w in (self.zmode_lbl, self.zmode_box, self.vehicle_btn):
                w.setVisible(bool(modes))
            self.mirror_btn.setVisible(self._paired() is not None)
            if not modes:
                return
            want = getattr(self, "_zmode_pref", _view6.DEFAULT_MODE)
            if want not in modes:
                want = self._zmode_pref = _view6.DEFAULT_MODE
            i = self.zmode_box.findData(want)
            if i >= 0 and self.zmode_box.currentIndex() != i:
                self.zmode_box.blockSignals(True)
                self.zmode_box.setCurrentIndex(i)
                self.zmode_box.blockSignals(False)
            self.vehicle_btn.setVisible(want in ("kmh", "mph"))
            if self.model.zmode != want:
                self.model.set_zmode(want, self._vehicle())
                self._fit_columns()

        def _toggle_transpose(self):
            """View > Swap X and Y axes: turn every table, in every tab.

            One switch, not a per-table choice: off shows each map as its
            definition stores it and every 1-D table along X -- the reference
            files are not even consistent about which way they store a curve
            (321 Nx1 against 1168 1xN), so that is a display choice, not a
            property of the data -- and on turns all of them. Remembered
            between sessions.
            """
            set_swap_axes(not swap_axes())
            self.win.swap_act.setChecked(swap_axes())
            self.follow_swap()

        def follow_swap(self):
            """Bring the table on screen in line with the swap switch."""
            e = self.model.entity
            if e is None:
                return
            before = self.model.transposed
            self._sync_transpose(e)
            if self.model.transposed != before:
                self._set_title(e)
                self._fit_columns()

        def _sync_transpose(self, e):
            """Show ``e`` the way the swap switch says: as stored (a 1-D
            table along X), or turned."""
            if e is None:
                return
            want = bool(self.model._default_transpose(e)) != swap_axes()
            if self.model.transposed != want:
                self.model.set_transposed(want)

        def _fit_columns(self):
            """Size the columns to what is actually in them, with a floor.

            Two reasons this cannot be left to Qt's default width. The values
            are not all the same length -- a ZF shift map holds 1500 in output
            speed and 6710.4 once it is shown as engine rpm -- and the ZF
            column headers are labels like "1 > 2" rather than indices, so a
            column can be wider than its own data. Both change under the Z
            mode selector, which is why this runs on every mode change as well
            as on every selection.

            Sizing samples rows rather than reading all of them. Qt's default
            is to scan every row of every column, which on the widest tables
            here (a 643-row curve, a 258x256 grid with a comparison) is
            hundreds of thousands of ``data()`` calls for a column width, and
            it ran on every click. A sample settles the width just as well:
            the values in one column are the same kind of number, so the
            widest of the first few hundred is the widest.
            """
            hdr = self.table.horizontalHeader()
            hdr.setResizeContentsPrecision(_FIT_SAMPLE_ROWS)
            self.table.resizeColumnsToContents()
            for c in range(self.model.columnCount()):
                if self.table.columnWidth(c) < _MIN_CELL_W:
                    self.table.setColumnWidth(c, _MIN_CELL_W)
            hdr.setStretchLastSection(False)

        def _paired(self):
            """This entity's other coding bank, or None."""
            e = self.model.entity
            if e is None:
                return None
            ents = self.primary.entities
            key = (id(ents), len(ents), self.primary.revision)
            if getattr(self, "_by_name_key", None) != key:
                # every parameter by name: built once, not on every click
                self._by_name = _view6.index_by_name(ents)
                self._by_name_key = key
            return _view6.paired_entity(e, self._by_name)

        def mirror_to_bank(self):
            e = self.model.entity
            other = self._paired()
            if e is None or other is None:
                return
            cells = _view6.mirror_cells(e, other)
            if not cells:
                QtWidgets.QMessageBox.information(
                    self, "Copy to other bank",
                    "%s already matches %s." % (other.name, e.name))
                return
            if QtWidgets.QMessageBox.question(
                    self, "Copy to other bank",
                    "Overwrite %d of %d cells in %s with the values from "
                    "%s?\n\nThe two coding banks are separate calibrations, "
                    "not copies of each other."
                    % (len(cells), e.rows * e.cols, other.name, e.name)
            ) != QtWidgets.QMessageBox.Yes:
                return
            with self.primary.transaction("copy %s to %s"
                                          % (e.name, other.name)):
                self.primary.edit_cells(other, cells)
            self.model.refresh()
            self._status()
            self.statusBar().showMessage(
                "Copied %d cells to %s" % (len(cells), other.name), 5000)

        def _zmode_changed(self):
            self._zmode_pref = self.zmode_box.currentData() or "raw"
            self._zmode_default = self._zmode_pref  # for tables opened next
            if self._tt_cur is not None:
                self._tt_cur.zmode = self._zmode_pref
            _settings().setValue("zf6hp/zmode", self._zmode_pref)
            self.vehicle_btn.setVisible(self._zmode_pref in ("kmh", "mph"))
            self.model.set_zmode(self._zmode_pref, self._vehicle())

        def _vehicle(self):
            """The vehicle the road-speed modes use.

            The calibration's own final drive wins where it states one and the
            matcher placed it byte-exactly -- reading it beats asking, and a
            wrong ratio silently skews every road-speed figure. The saved
            setting is the fallback and the tyre always comes from there,
            since no calibration knows what is fitted.
            """
            st = _settings()
            try:
                saved = _view6.Vehicle(
                    axle_ratio=float(st.value("zf6hp/axle", 4.16)),
                    tyre=str(st.value("zf6hp/tyre", "255/40R19")))
            except (TypeError, ValueError):
                saved = _view6.Vehicle()
            if st.value("zf6hp/axle_override", False) in (True, "true"):
                return saved
            return _view6.vehicle_of(self.primary, saved)

        def open_vehicle(self):
            """Ask for the final drive and tyre size -- all road speed needs on
            top of what the map already says -- and the vehicle's mass, which
            the shift editor's full-load optimum works the shift lead from."""
            cur = self._vehicle()
            stated = _view6.axle_ratio_of(self.primary)
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Vehicle")
            form = QtWidgets.QFormLayout(dlg)
            axle = QtWidgets.QDoubleSpinBox()
            axle.setRange(1.0, 10.0)
            axle.setDecimals(3)
            axle.setSingleStep(0.01)
            axle.setValue(cur.axle_ratio)
            tyre = QtWidgets.QLineEdit(cur.tyre)
            tyre.setPlaceholderText("225/50R17")
            mass = QtWidgets.QSpinBox()
            mass.setRange(500, 5000)
            mass.setSingleStep(10)
            mass.setSuffix(" kg")
            try:
                mass.setValue(int(float(_settings().value("zf6hp/mass", 1800) or 1800)))
            except (TypeError, ValueError):
                mass.setValue(1800)
            override = QtWidgets.QCheckBox(
                "Use my ratio instead of the calibration's")
            override.setChecked(
                _settings().value("zf6hp/axle_override", False)
                in (True, "true"))
            override.setVisible(stated is not None)
            note = QtWidgets.QLabel(
                ("This calibration states a final drive of %.3f, which is "
                 "used unless you override it.\n" % stated if stated
                 else "This calibration does not state a final drive.\n")
                + "Final drive and tyre show output-shaft speed as road speed;\n"
                  "the mass (with driver) is used by the shift editor's\n"
                  "full-load optimum. Nothing in the binary is changed by these.")
            note.setStyleSheet("color:#888")
            form.addRow("Final drive ratio:", axle)
            form.addRow("Tyre size:", tyre)
            form.addRow("Vehicle mass:", mass)
            if stated is not None:
                form.addRow("", override)
            form.addRow(note)
            bb = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
            form.addRow(bb)
            bb.rejected.connect(dlg.reject)

            def accept():
                try:
                    _view6.Vehicle(axle.value(), tyre.text()).circumference_m
                except ValueError as exc:
                    QtWidgets.QMessageBox.warning(dlg, "Tyre size", str(exc))
                    return
                dlg.accept()

            bb.accepted.connect(accept)
            if dlg.exec() != QtWidgets.QDialog.Accepted:
                return
            st = _settings()
            st.setValue("zf6hp/axle", axle.value())
            st.setValue("zf6hp/tyre", tyre.text().strip())
            st.setValue("zf6hp/mass", mass.value())
            st.setValue("zf6hp/axle_override",
                        bool(override.isChecked()) if stated is not None
                        else False)
            self.model.set_zmode(self.model.zmode, self._vehicle())

        def open_surface(self):
            from . import gui_surface3d as _s3d     # the panel above the table
            return _s3d.open_for(self)

        def open_logtune(self):
            if self.primary is None:
                return
            try:
                from .logtune.gui_logtune import LogTuneDialog
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Tune from logs", str(ex))
                return
            self.open_tool("logtune", lambda: LogTuneDialog(self))

        def load_backup(self, flash, eeprom, path=None):
            """Open a flash+EEPROM pair (an ECU full backup, saved as .edc15):
            the flash becomes the editable primary, the EEPROM is held for
            the EEPROM editor and goes into the .edc15 when it is saved. (An
            old KWHack .edc15bak is imported into a project instead.)"""
            name = _os.path.basename(path) if path else \
                "ECU-backup-%s" % __import__("datetime").datetime.now().strftime("%H%M%S")
            self._defer_info = True
            try:
                ok = self.load_from_image(flash, name)
            finally:
                self._defer_info = False
            if not ok or self.primary is None:
                return False
            self._eeprom = bytes(eeprom)
            self._eeprom_dirty = False
            self._backup_path = None
            if path:
                from .recent import add_recent
                self.primary.binfile.name = name
                if not path.lower().endswith(".edc15bak"):
                    self._untitled = False
                    self.primary.path = path
                add_recent(path)            # (the picker records its folder)
                self._rebuild_recent()
            self._refresh_enabled()
            self._status()
            self.show_file_info(loaded=True)
            return True

        def export_backup_part(self, which):
            """Write the flash (as-is, current in-memory bytes) or the held
            EEPROM out of a loaded backup as a plain .bin."""
            from .recent import get_last_dir, set_last_dir
            if which == "eeprom":
                data = getattr(self, "_eeprom", None)
                if data is None:
                    QtWidgets.QMessageBox.information(
                        self, "Export EEPROM", "No EEPROM is loaded (open a full "
                        "backup or run one first).")
                    return
                suffix = "_EEPROM.bin"
            else:
                data = bytes(self.primary.binfile.data)
                suffix = "_flash.bin"
            base = _os.path.splitext(self.primary.binfile.name)[0].replace(" (unsaved)", "")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export %s" % which, _os.path.join(get_last_dir("export"), base + suffix),
                "Binary (*.bin);;All files (*)")
            path = _ensure_ext(path, ".bin")
            if not path:
                return
            with open(path, "wb") as fh:
                fh.write(data)
            set_last_dir(path, "export")
            self.statusBar().showMessage("Exported %s to %s" % (which, path), 6000)

        def export_dim_mpc(self):
            """Write the loaded .dim's internal flash out as the plain,
            uncompressed 0x81000 (516 KB) .mpc image -- the same form the
            Reference/Binaries KN.MPC samples and ``ovtsuite checksum`` use,
            and what ``ovtsuite dim-extract-mpc`` writes by default (see
            ovtsuite/dim.py / docs/CONTAINERS.md)."""
            from .recent import get_last_dir, set_last_dir
            data = None
            if self.primary is not None:
                for _n, _b in self.primary.region_buffers():
                    if _n.startswith("MPC"):
                        data = _b; break
            if data is None:
                QtWidgets.QMessageBox.information(
                    self, "Export internal flash (MPC)",
                    "The loaded file has no MPC/internal-flash block -- open a "
                    ".dim, .edc16, or .sgo that carries it (File > Open primary).")
                return
            base = _os.path.splitext(self.primary.binfile.name)[0].replace(" (unsaved)", "")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export internal flash (MPC)",
                _os.path.join(get_last_dir("export"), base + "_internal.mpc"),
                "MPC internal flash (*.mpc);;All files (*)")
            path = _ensure_ext(path, ".mpc")
            if not path:
                return
            with open(path, "wb") as fh:
                fh.write(data)
            set_last_dir(path, "export")
            self.statusBar().showMessage("Exported internal flash (MPC) to %s" % path, 6000)

        def export_sgo_binary(self):
            """Write the loaded .sgo's current (tuned) content out as a
            flat, plain .bin -- the same addressing BinaryFile already uses
            internally (see ovtsuite.sgo.image), so it opens directly as a
            normal binary elsewhere. Unlike Save/Save As (which re-packs the
            original .sgo container, see TunerSession.save), this discards
            the container -- any section OVT-Suite couldn't decode stays
            0xFF filler in the export, same as it shows on screen."""
            from .recent import get_last_dir, set_last_dir
            container = getattr(self.primary, "_sgo_container", None) if self.primary else None
            if container is None:
                QtWidgets.QMessageBox.information(
                    self, "Export as plain binary",
                    "The loaded file isn't a .sgo container -- open one first "
                    "(File > Open primary).")
                return
            data = bytes(self.primary.binfile.data)
            base = _os.path.splitext(self.primary.binfile.name)[0].replace(" (unsaved)", "")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export as plain binary",
                _os.path.join(get_last_dir("export"), base + ".bin"),
                "Binary files (*.bin);;All files (*)")
            path = _ensure_ext(path, ".bin")
            if not path:
                return
            with open(path, "wb") as fh:
                fh.write(data)
            set_last_dir(path, "export")
            self.statusBar().showMessage("Exported plain binary to %s" % path, 6000)

        def export_dim_eeprom(self):
            """Write the loaded .dim's EEPROM chunk out as a plain .e2p
            file."""
            from . import dim as dim_mod
            from .recent import get_last_dir, set_last_dir
            chunks = getattr(self.primary, "_dim_chunks", None) if self.primary else None
            if chunks is None:
                QtWidgets.QMessageBox.information(
                    self, "Export EEPROM",
                    "The loaded file isn't a .dim container -- open one first "
                    "(File > Open primary).")
                return
            try:
                data = dim_mod.eeprom(chunks)
            except KeyError:
                QtWidgets.QMessageBox.information(
                    self, "Export EEPROM", "This .dim has no EEPROM chunk.")
                return
            base = _os.path.splitext(self.primary.binfile.name)[0].replace(" (unsaved)", "")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export EEPROM",
                _os.path.join(get_last_dir("export"), base + "_EEPROM.e2p"),
                "EEPROM image (*.e2p);;Binary files (*.bin);;All files (*)")
            path = _ensure_ext(path, ".e2p")
            if not path:
                return
            with open(path, "wb") as fh:
                fh.write(data)
            set_last_dir(path, "export")
            self.statusBar().showMessage("Exported EEPROM to %s" % path, 6000)

        def _container_eeprom(self):
            """The EEPROM a loaded native container (.edc15 / .edc16) carries,
            with any in-place edits (the hex editor's Region: EEPROM), or None.
            A .dim's EEPROM has its own export (EEPROM (.dim)...)."""
            p = self.primary
            if (p is None or getattr(p, "_dim_chunks", None) is not None
                    or not p.has_eeprom()):
                return None
            return dict(p.region_buffers()).get(p.EEPROM_REGION)

        def _container_eeprom_adopt(self, data):
            """Write EEPROM editor bytes into the container's EEPROM region as
            one undoable edit (the same buffer the hex editor edits)."""
            p = self.primary
            if p is not None and p.edit_region(p.EEPROM_REGION, 0,
                                               bytes(data)) is not None:
                self._status()

        def export_container_eeprom(self):
            """Write the EEPROM a loaded .edc15 / .edc16 carries (with any
            edits) out as a plain image."""
            from .recent import get_last_dir, set_last_dir
            data = self._container_eeprom()
            if data is None:
                QtWidgets.QMessageBox.information(
                    self, "Export EEPROM",
                    "The loaded file carries no EEPROM.")
                return
            base = _os.path.splitext(self.primary.binfile.name)[0].replace(
                " (unsaved)", "")
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export EEPROM",
                _os.path.join(get_last_dir("export"), base + "_EEPROM.bin"),
                "EEPROM image (*.bin);;All files (*)")
            path = _ensure_ext(path, ".bin")
            if not path:
                return
            with open(path, "wb") as fh:
                fh.write(data)
            set_last_dir(path, "export")
            self.statusBar().showMessage("Exported EEPROM to %s" % path, 6000)

        def _eeprom_editor_save(self, data):
            """**Apply** in the EEPROM editor for the EEPROM this tab holds:
            the bytes go into the tab (saved with the project, like any
            edit)."""
            if bytes(data) != bytes(self._eeprom or b""):
                self._eeprom = bytes(data)
                self._eeprom_dirty = True
            self._status()

        def open_eeprom_editor(self):
            """Tools > EEPROM editor: the EEPROM this tab holds (a backup's,
            or the project set's EEPROM part) -- edits go back into it as one
            undoable edit -- else an EEPROM image picked from disk."""
            from . import gui_eeprom
            from .recent import get_last_dir, set_last_dir
            name = "%s [EEPROM]" % (self.primary.binfile.name
                                    if self.primary else "backup")
            held = getattr(self, "_eeprom", None)
            if held is not None:
                dlg = gui_eeprom.open_editor(self, held, name=name,
                                             on_save=self._eeprom_editor_save)
                if dlg is None:
                    return
                dlg.exec()
                if dlg.data != held:
                    self._eeprom = bytes(dlg.data)
                    self._eeprom_dirty = True
                    self._status()
                return
            inner = self._container_eeprom()
            if inner is not None and gui_eeprom.layouts_for(inner):
                def _adopt(data):
                    if bytes(data) != bytes(self._container_eeprom() or b""):
                        self._container_eeprom_adopt(data)
                dlg = gui_eeprom.open_editor(self, inner, name=name,
                                             on_save=_adopt)
                if dlg is None:
                    return
                dlg.exec()
                if dlg.data != self._container_eeprom():
                    self._container_eeprom_adopt(dlg.data)
                return
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open an EEPROM image", get_last_dir("eeprom"),
                gui_eeprom.FILTER)
            if not path:
                return
            with open(path, "rb") as fh:
                data = fh.read()
            set_last_dir(_os.path.dirname(path), "eeprom")
            dlg = gui_eeprom.open_editor(self, data, path=path)
            if dlg is not None:
                dlg.exec()

        def show_file_info(self, session=None, loaded=False):
            """Identification strings + what the parser found, for the primary
            (default) or the comparison session, with the project details of
            a project tab (gui_fileinfo). ``loaded`` titles it as a
            just-loaded summary, shown only while "Show this when a file is
            opened" is ticked; otherwise it is an on-demand info view."""
            if session is False or session is True:   # stray QAction 'checked'
                session = None
            from . import gui_fileinfo
            return gui_fileinfo.show(self, session, loaded=loaded)

        def load_from_image(self, data, name):
            """Open an in-memory 2 MB image (an ECU read) as the current file,
            unsaved: Save will prompt for a location (Save As)."""
            import tempfile
            import os as _os2
            fd, tmp = tempfile.mkstemp(suffix=".bin", prefix="edc16-read-")
            _os2.close(fd)
            with open(tmp, "wb") as fh:
                fh.write(data)
            try:
                loaded = TunerSession(tmp)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Flashing",
                                               "Read image did not load: %s" % ex)
                return False
            _gp.leaving(self)               # the old one's definition snapshot
            self.shutdown()                 # tool windows were the old file's
            open_tables = self._drop_tables()
            dropped = self._carry_comparison(loaded)
            self.primary = loaded
            self._leave_project()
            try:
                loaded.correct_program_area = program_area_pref()
            except Exception:
                pass
            self.primary.binfile.name = "%s (unsaved)" % name
            self._untitled = True
            self._backup_path = None
            self._eeprom = None
            self._eeprom_dirty = False
            self._captured_temp = tmp
            self.model.set_entity(None, None)
            self.model.session = self.primary
            self._show_comparison(None, None)
            self.title.setText("Select a parameter")
            self.desc.setText("")
            self._refresh_enabled()
            self._auto_advanced_if_needed()
            self._populate()
            self.restore_tables(open_tables["tables"],
                                open_tables["table_active"])
            self._status()
            self._say_dropped(dropped)
            if not getattr(self, "_defer_info", False):
                self.show_file_info()
            return True

        def open_hex(self):
            if self.primary is None:
                return
            _extra = []
            _ee = getattr(self, "_eeprom", None)
            if _ee is not None and any(
                    n == self.primary.EEPROM_REGION
                    for n, _b in self.primary.region_buffers()):
                _ee = None      # the saved container already exposes it
            if _ee is not None:
                # Editable in the hex view: edits go straight into the held
                # EDC15 EEPROM image (a .bin backup save then writes it out).
                _eebuf = bytearray(_ee)
                def _ee_writer(off, b, _eebuf=_eebuf):
                    changed = False
                    for i, nb in enumerate(bytes(b)):
                        a = off + i
                        if a < len(_eebuf) and _eebuf[a] != (nb & 0xFF):
                            _eebuf[a] = nb & 0xFF
                            changed = True
                    if changed:
                        self._eeprom = bytes(_eebuf)
                        self._eeprom_dirty = True
                    return changed
                _extra.append(("EEPROM (EDC15)", _eebuf, _ee_writer))
            return self.open_tool("hex", lambda: HexDialog(
                self.primary, self.comparison, self, extra_regions=_extra))

        def _apply_spans(self):
            """Span the comparison banner row across the grid."""
            self.table.clearSpans()
            sep = self.model.sep_row()
            if sep is not None and self.model.columnCount() > 1:
                self.table.setSpan(sep, 0, 1, self.model.columnCount())

        def _note_no_counterpart(self, e):
            """Record, once per table, that the comparison has no match for it.

            A table with no counterpart shows no comparison block at all, which
            is indistinguishable from never having opened a comparison. Saying so
            once in the log is what turned "the comparison table is not visible"
            into something locatable.
            """
            seen = getattr(self, "_no_cp_seen", None)
            if seen is None:
                seen = self._no_cp_seen = set()
            key = (id(self.comparison), e.name or "", int(e.addr))
            if key in seen:
                return
            seen.add(key)
            what = e.name or (getattr(e, "label", "") or "this table")
            try:
                self.primary.log(
                    "comparison: %s has no counterpart for %s (%dx%d at 0x%X) -- "
                    "no comparison values will be shown for it"
                    % (self.comparison.binfile.name, what, e.rows, e.cols, e.addr))
            except Exception:
                pass


        def _show_comparison(self, e, cmp_e):
            """Keep the model's comparison banner in step with the loaded
            comparison; close the differences window when it is dropped."""
            self.model.cmp_name = (self.comparison.binfile.name
                                   if self.comparison is not None else "")
            if self.comparison is None and getattr(self, "_diff_dlg", None):
                self._diff_dlg.close()          # its rows are stale now
                self._diff_dlg = None

        def _find_tree_item(self, entity):
            """The tree row for ``entity``, or None when there is none or the
            current filters hide it."""
            item = getattr(self, "_item_of", {}).get(id(entity))
            if item is None or item.isHidden():
                return None
            return item

        def show_entity(self, entity):
            """Show an entity by object (used by dialogs), the way a click in
            the tree does: in its own table tab, or the preview tab. The
            tree's filter is cleared when it hides the entity."""
            item = self._find_tree_item(entity)
            hidden_group = (_is_advanced(entity)
                            and not self.advanced.isChecked())
            if item is None and (self.filter.text() or self.tier_box.currentIndex()
                                 or self.diff_only.isChecked() or hidden_group):
                self.filter.blockSignals(True)
                self.tier_box.blockSignals(True)
                self.diff_only.blockSignals(True)
                self.advanced.blockSignals(True)
                self.filter.clear()
                self.tier_box.setCurrentIndex(0)
                self.diff_only.setChecked(False)
                if hidden_group:
                    self.advanced.setChecked(True)
                self.filter.blockSignals(False)
                self.tier_box.blockSignals(False)
                self.diff_only.blockSignals(False)
                self.advanced.blockSignals(False)
                self._populate()
                item = self._find_tree_item(entity)
            return self.open_table(entity)

    SWAP_KEY = "view/swap_xy"
    ADVANCED_KEY = "view/advanced_groups"

    def swap_axes() -> bool:
        """View > Swap X and Y axes -- one switch for every table."""
        try:
            return bool(_settings().value(SWAP_KEY, False, type=bool))
        except Exception:
            return False

    def set_swap_axes(on: bool):
        _settings().setValue(SWAP_KEY, bool(on))

    PROGRAM_AREA_KEY = "zf6hp/correct_program_area"

    def program_area_pref() -> bool:
        """Tools > ZF 6HP > Correct program-area checksums -- off by default. When
        on, a ZF 6HP save/flash also rewrites the program/boot-area descriptor
        self-CRCs (zlib CRC-32), not just the calibration checksum."""
        try:
            return bool(_settings().value(PROGRAM_AREA_KEY, False, type=bool))
        except Exception:
            return False

    def set_program_area_pref(on: bool):
        _settings().setValue(PROGRAM_AREA_KEY, bool(on))

    class _TableTab:
        """One open table in a file tab, and what it remembers."""
        __slots__ = ("entity", "pinned", "heat", "zmode", "view")

        def __init__(self, entity, pinned=False, heat=False, zmode=None):
            self.entity = entity
            self.pinned = pinned        # False: the preview tab
            self.heat = heat            # heatmap shading on
            self.zmode = zmode          # ZF "Show Z as" (None: not chosen)
            self.view = None            # scroll and selection, once left

    class TableTabBar(QtWidgets.QTabBar):
        """The tables open in one file tab. The preview tab -- the one the
        next click in the tree replaces -- is drawn in italics. Middle-click
        closes a tab."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setTabsClosable(True)
            self.setMovable(True)
            self.setExpanding(False)
            self.setDocumentMode(True)
            self.setUsesScrollButtons(True)
            self.setElideMode(QtCore.Qt.TextElideMode.ElideRight)
            self.setSelectionBehaviorOnRemove(
                QtWidgets.QTabBar.SelectionBehavior.SelectLeftTab)

        def is_preview(self, i) -> bool:
            t = self.tabData(i)
            return t is not None and not getattr(t, "pinned", True)

        def tabSizeHint(self, i):
            sz = super().tabSizeHint(i)
            if self.is_preview(i):          # room for the slant
                sz.setWidth(sz.width() + 4)
            return sz

        def paintEvent(self, _ev):
            painter = QtWidgets.QStylePainter(self)
            cur = self.currentIndex()
            order = [i for i in range(self.count()) if i != cur]
            if cur >= 0:
                order.append(cur)
            base = painter.font()
            for i in order:
                opt = QtWidgets.QStyleOptionTab()
                self.initStyleOption(opt, i)
                painter.drawControl(
                    QtWidgets.QStyle.ControlElement.CE_TabBarTabShape, opt)
                f = QtGui.QFont(base)
                f.setItalic(self.is_preview(i))
                painter.setFont(f)
                painter.drawControl(
                    QtWidgets.QStyle.ControlElement.CE_TabBarTabLabel, opt)
            painter.setFont(base)

        def mouseReleaseEvent(self, ev):
            if ev.button() == QtCore.Qt.MouseButton.MiddleButton:
                i = self.tabAt(ev.position().toPoint())
                if i >= 0:
                    self.tabCloseRequested.emit(i)
                    return
            super().mouseReleaseEvent(ev)

    class TabBar(QtWidgets.QTabBar):
        """Tabs marked by what they hold -- ECU blue, TCU amber -- so a
        glance says which kind of file each one is. The colour is the tab's
        ``tabData``; a tab without one (an empty tab) is drawn plainly. The
        current tab stands out: the style's own selected look, a bold label
        and a thick bar of its colour along the top; the others get only a
        thin line of theirs and slightly dimmed text.
        Middle-click closes a tab, as in a browser."""

        #: the current tab's bar and the others' line, in pixels
        CURRENT_BAR, OTHER_LINE = 4, 2

        def _arrow_region(self):
            """Where the scroll arrows sit (empty when they are hidden). The
            tabs are painted here by hand, so they must keep out of it -- they
            were drawn under the arrows, which hid both. Each arrow on its
            own: a style may put one at each end (Windows 11 does), and the
            span between them is the tabs."""
            reg = QtGui.QRegion()
            for b in self.findChildren(QtWidgets.QToolButton):
                if b.isVisible() and b.parent() is self:
                    if not b.autoFillBackground():
                        b.setAutoFillBackground(True)   # opaque, not see-through
                    reg = reg.united(QtGui.QRegion(b.geometry()))
            return reg

        def _bold(self) -> QtGui.QFont:
            f = QtGui.QFont(self.font())
            f.setBold(True)
            return f

        def tabSizeHint(self, i):
            # every tab has room for its label in bold, so the tabs don't
            # shift as the current one changes
            sz = super().tabSizeHint(i)
            text = self.tabText(i)
            extra = (QtGui.QFontMetrics(self._bold()).horizontalAdvance(text)
                     - self.fontMetrics().horizontalAdvance(text))
            if extra > 0:
                sz.setWidth(sz.width() + extra)
            return sz

        def minimumTabSizeHint(self, i):
            return self.tabSizeHint(i)

        def paintEvent(self, _ev):
            painter = QtWidgets.QStylePainter(self)
            arrows = self._arrow_region()
            if not arrows.isEmpty():
                painter.setClipRegion(QtGui.QRegion(self.rect()).subtracted(arrows))
            cur = self.currentIndex()
            order = [i for i in range(self.count()) if i != cur]
            if cur >= 0:
                order.append(cur)          # the current tab overlaps last
            base, bold = painter.font(), self._bold()
            for i in order:
                opt = QtWidgets.QStyleOptionTab()
                self.initStyleOption(opt, i)
                painter.drawControl(
                    QtWidgets.QStyle.ControlElement.CE_TabBarTabShape, opt)
                col = self.tabData(i)
                if isinstance(col, QtGui.QColor):
                    r = opt.rect.adjusted(2, 1, -2, 0)
                    h = self.CURRENT_BAR if i == cur else self.OTHER_LINE
                    painter.fillRect(
                        QtCore.QRect(r.left(), r.top(), r.width(), h), col)
                if i == cur:
                    painter.setFont(bold)
                    opt.fontMetrics = QtGui.QFontMetrics(bold)
                else:
                    painter.setFont(base)
                    pal = opt.palette
                    for role in (QtGui.QPalette.ColorRole.WindowText,
                                 QtGui.QPalette.ColorRole.ButtonText):
                        fg = pal.color(role)
                        bg = pal.color(QtGui.QPalette.ColorRole.Window)
                        a = 0.68                    # how much of the text
                        pal.setColor(role, QtGui.QColor(
                            round(fg.red() * a + bg.red() * (1 - a)),
                            round(fg.green() * a + bg.green() * (1 - a)),
                            round(fg.blue() * a + bg.blue() * (1 - a))))
                    opt.palette = pal
                painter.drawControl(
                    QtWidgets.QStyle.ControlElement.CE_TabBarTabLabel, opt)
            painter.setFont(base)

        def mouseReleaseEvent(self, ev):
            if ev.button() == QtCore.Qt.MouseButton.MiddleButton:
                i = self.tabAt(ev.position().toPoint())
                if i >= 0:
                    self.tabCloseRequested.emit(i)
                    return
            super().mouseReleaseEvent(ev)

    def _swatch(color, size=12):
        """A small square of a tab's tint, for lists of tabs."""
        pm = QtGui.QPixmap(size, size)
        pm.fill(color if color is not None else QtGui.QColor(0, 0, 0, 0))
        return QtGui.QIcon(pm)

    class UnsavedDialog(QtWidgets.QDialog):
        """Before the window closes: what has unsaved changes, ticked to be
        saved. A project is one line with its edited tabs (and its own
        changes -- branches, logs, attachments) under it: tick the project to
        save all of it, or only some of its tabs. Every project is written
        once. Save selected / Don't save / Cancel."""

        def __init__(self, workspaces, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Unsaved changes")
            self.choice = None
            v = QtWidgets.QVBoxLayout(self)
            v.addWidget(QtWidgets.QLabel("Save these changes before closing?"))
            self.tree = QtWidgets.QTreeWidget()
            self.tree.setHeaderHidden(True)
            Checked = QtCore.Qt.CheckState.Checked
            role = QtCore.Qt.ItemDataRole.UserRole
            groups = {}
            n = 0
            for ws in workspaces:
                doc = getattr(ws, "doc", None)      # a project's own changes
                ref = getattr(ws, "project_ref", None)
                if doc is None and ref is not None:
                    doc = ref.doc
                if doc is None:                     # a tab that is no project
                    it = QtWidgets.QTreeWidgetItem([ws.tab_label()])
                    it.setIcon(0, _swatch(ws.tint()))
                    it.setToolTip(0, ws.tab_tooltip())
                    it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(0, Checked)
                    it.setData(0, role, ("tab", ws))
                    self.tree.addTopLevelItem(it)
                    n += 1
                    continue
                top = groups.get(id(doc))
                if top is None:
                    top = QtWidgets.QTreeWidgetItem(
                        ["Project %s" % doc.title])
                    f = top.font(0)
                    f.setBold(True)
                    top.setFont(0, f)
                    top.setToolTip(0, doc.path or "not saved yet")
                    top.setFlags(top.flags()
                                 | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                                 | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
                    top.setData(0, role, ("project", doc))
                    self.tree.addTopLevelItem(top)
                    groups[id(doc)] = top
                    n += 1
                if ref is not None:
                    label = "%s · %s" % (ref.set.name, ref.branch.name)
                    data = ("tab", ws)
                else:
                    label = "The project's own changes (branches, logs, ...)"
                    data = ("doc", doc)
                ch = QtWidgets.QTreeWidgetItem([label])
                if ref is not None:
                    ch.setIcon(0, _swatch(ws.tint()))
                ch.setFlags(ch.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                ch.setCheckState(0, Checked)
                ch.setData(0, role, data)
                top.addChild(ch)
            for top in groups.values():
                doc = top.data(0, role)[1]
                if doc.project.modified_since_save and not any(
                        top.child(i).data(0, role)[0] == "doc"
                        for i in range(top.childCount())):
                    ch = QtWidgets.QTreeWidgetItem(
                        ["The project's own changes (branches, logs, ...)"])
                    ch.setFlags(ch.flags()
                                | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    ch.setCheckState(0, Checked)
                    ch.setData(0, role, ("doc", doc))
                    top.addChild(ch)
                top.setExpanded(True)
            v.addWidget(self.tree)
            bb = QtWidgets.QDialogButtonBox()
            save = bb.addButton("Save selected",
                                QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            discard = bb.addButton("Don't save",
                                   QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
            bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            save.setDefault(True)
            save.clicked.connect(lambda: self._done("save"))
            discard.clicked.connect(lambda: self._done("discard"))
            bb.rejected.connect(self.reject)
            v.addWidget(bb)
            self.resize(560, 140 + 22 * min(len(workspaces) + n, 14))

        def _done(self, choice):
            self.choice = choice
            self.accept()

        def plan(self):
            """What to save: ``[(doc, [tabs])]`` -- each project once, with
            the ticked tabs of it (an empty list: only its own changes) --
            and ``[tab]`` for tabs that are no project."""
            role = QtCore.Qt.ItemDataRole.UserRole
            Unchecked = QtCore.Qt.CheckState.Unchecked
            projects, tabs = [], []
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                kind, obj = top.data(0, role)
                if kind == "tab":
                    if top.checkState(0) != Unchecked:
                        tabs.append(obj)
                    continue
                picked = [top.child(j) for j in range(top.childCount())
                          if top.child(j).checkState(0) != Unchecked]
                if picked:
                    projects.append((obj, [c.data(0, role)[1] for c in picked
                                           if c.data(0, role)[0] == "tab"]))
            return projects, tabs

        def selected(self):
            """Every ticked tab (and project-changes entry), flat."""
            projects, tabs = self.plan()
            return [t for _d, ts in projects for t in ts] + tabs

    class RestoreProjectsDialog(QtWidgets.QDialog):
        """At startup: the projects that were open last time, ticked, so only
        the ones needed are reopened (each with the tabs it had). A file that
        was not a project yet is listed on its own; one that is gone, or a
        read never saved, is listed but can't be ticked."""

        def __init__(self, entries, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Reopen projects")
            self.entries = entries
            self.groups = _gp.session_groups(entries)
            v = QtWidgets.QVBoxLayout(self)
            lead = QtWidgets.QLabel(
                "These projects were open when %s last closed. Each reopens "
                "with the tabs it had; loading takes a few seconds per tab, so "
                "tick only the ones you need." % branding.APP_NAME)
            lead.setWordWrap(True)
            v.addWidget(lead)
            self.list = QtWidgets.QListWidget()
            for gi, g in enumerate(self.groups):
                if g["kind"] == "project":
                    label = g["title"]
                    if g["vehicle"] and g["vehicle"] != g["title"]:
                        label += "  --  %s" % g["vehicle"]
                    if g["tabs"]:
                        label += "   (%s)" % ", ".join(g["tabs"])
                    if not g["ok"]:
                        label += "   (project not found)"
                else:
                    label = "%s   (a file, not in a project yet)" % g["title"]
                    if not g["path"]:
                        label = "%s   (read from a module, never saved)" \
                            % g["title"]
                    elif not g["ok"]:
                        label += "   (file not found)"
                col = platform_badge(g["platforms"][0])[2] \
                    if g["ok"] and g["platforms"] else None
                it = QtWidgets.QListWidgetItem(_swatch(col), label)
                tip = [_gp.real_path(g["path"]) or "not on disk"]
                if g["kind"] == "project" and g["tabs"]:
                    tip.append("Tabs: %s" % ", ".join(g["tabs"]))
                it.setToolTip("\n".join(tip))
                flags = it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                if not g["ok"]:
                    flags &= ~QtCore.Qt.ItemFlag.ItemIsEnabled
                it.setFlags(flags)
                it.setCheckState(QtCore.Qt.CheckState.Checked if g["ok"]
                                 else QtCore.Qt.CheckState.Unchecked)
                it.setData(QtCore.Qt.ItemDataRole.UserRole, gi)
                self.list.addItem(it)
            v.addWidget(self.list)
            row = QtWidgets.QHBoxLayout()
            for text, state in (("All", QtCore.Qt.CheckState.Checked),
                                ("None", QtCore.Qt.CheckState.Unchecked)):
                b = QtWidgets.QPushButton(text)
                b.setAutoDefault(False)
                b.clicked.connect(lambda _c=False, s=state: self._tick_all(s))
                row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
            bb = QtWidgets.QDialogButtonBox()
            go = bb.addButton("Reopen selected",
                              QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
            bb.addButton("Start empty",
                         QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
            go.setDefault(True)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            v.addWidget(bb)
            # wide enough for the longest line (vehicle and tabs), within
            # the screen; tall enough for a dozen projects
            want = self.list.sizeHintForColumn(0) + 70
            scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
            cap = int(scr.availableGeometry().width() * 0.8) if scr else 1200
            self.resize(max(640, min(want, cap)),
                        190 + 28 * min(len(self.groups), 12))

        def _tick_all(self, state):
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled:
                    it.setCheckState(state)

        def chosen(self):
            """Indexes (into ``entries``) of the tabs of the ticked projects
            and files, in last session's order."""
            out = []
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.checkState() == QtCore.Qt.CheckState.Checked:
                    out += self.groups[it.data(QtCore.Qt.ItemDataRole.UserRole)
                                       ]["indexes"]
            return sorted(out)

    #: QSettings key holding the tabs open at the last close (JSON list).
    TABS_KEY = "tabs/last_session"
    #: QSettings key holding recently closed tabs, newest first (JSON list).
    CLOSED_KEY = "tabs/closed_history"
    #: How many closed tabs File > Restore tab from history keeps (the
    #: default of Options > General; :func:`_closed_max` reads the option).
    CLOSED_MAX = 15

    def _closed_max() -> int:
        from . import options as _opt
        return _opt.get("tabs/closed_max")

    class MainWindow(QtWidgets.QMainWindow):
        """The window: menus, a tab per set of binaries, a status bar.

        Everything about a file -- the primary, its comparison, the tree, the
        table, the tool windows -- lives in that tab's :class:`Workspace`, and
        each menu action runs on the active one. Attributes the window does
        not have itself (``primary``, ``model``, ``table``...) resolve to the
        active tab's, so code written against the single-file window -- the
        flashing dialog, the tests -- keeps working unchanged.
        """

        #: How often open tabs are checked for edits made elsewhere (ms).
        POLL_MS = 300

        def __init__(self, primary: Optional[TunerSession] = None,
                     comparison: Optional[TunerSession] = None):
            super().__init__()
            self.setWindowTitle(branding.title())
            self.setWindowIcon(app_icon())
            self.resize(1200, 760)
            self._quiet_close = False
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.setTabBar(TabBar())
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.setDocumentMode(True)
            self.tabs.setElideMode(QtCore.Qt.TextElideMode.ElideNone)
            self.tabs.tabBar().setExpanding(False)
            # scroll arrows rather than a wider window when the tabs run out
            # of room (some styles default to no arrows)
            self.tabs.setUsesScrollButtons(True)
            self.tabs.tabCloseRequested.connect(self._close_tab_at)
            self.tabs.currentChanged.connect(self._on_tab_changed)
            plus = self._plus_btn = QtWidgets.QToolButton()
            plus.setText("+")
            plus.setAutoRaise(True)
            plus.setToolTip("Import a binary, project or definition (the "
                            "File > Import menu)")
            plus.clicked.connect(self._plus_clicked)
            # every open tab in a list, for when they no longer all fit
            lst = self._tab_list_btn = QtWidgets.QToolButton()
            lst.setArrowType(QtCore.Qt.ArrowType.DownArrow)
            lst.setAutoRaise(True)
            lst.setToolTip("All open tabs")
            lst.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
            tab_menu = QtWidgets.QMenu(lst)
            tab_menu.aboutToShow.connect(lambda: self._fill_tab_list(tab_menu))
            lst.setMenu(tab_menu)
            corner = QtWidgets.QWidget()
            ch = QtWidgets.QHBoxLayout(corner)
            ch.setContentsMargins(0, 0, 0, 0)
            ch.setSpacing(0)
            ch.addWidget(lst)
            ch.addWidget(plus)
            self.tabs.setCornerWidget(corner, QtCore.Qt.Corner.TopRightCorner)
            self.setCentralWidget(self.tabs)
            #: the Find dock; one panel for every tab (Edit > Find, Ctrl+F)
            self.search_dock = _gui_search.install(
                self, title_fn=_english, tier_name=tier_name,
                settings=_settings)
            self._status_label = QtWidgets.QLabel()
            # clipped rather than widening the window; the tooltip has it all
            self._status_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Preferred)
            self.statusBar().addWidget(self._status_label, 1)
            # Qt hides the label for a message only if it is visible then: a
            # message shown before the window is (a project opened at start)
            # would be drawn over it
            self.statusBar().messageChanged.connect(
                lambda msg: self._status_label.setVisible(not msg))
            # the selected cells' address, at the right
            from . import gui_address
            gui_address.install(self)
            self._prev_ws = None
            self._make_actions()
            self._add_workspace(primary, comparison)
            self._watch = QtCore.QTimer(self)
            self._watch.setInterval(self.POLL_MS)
            self._watch.timeout.connect(self._tick)
            self._watch.start()
            _gp.install_autosave(self)      # crash recovery for open projects
            _gp.install_disk_watch(self)    # .ovt files changed by other programs
            from . import gui_options
            gui_options.install(self)       # Tools > Options, toolbar, shortcuts
            _gui_ai.install(self)           # AI apps: shared tabs, proposals
            self.setAcceptDrops(True)       # files dropped on the window
            self._refresh_enabled()
            self._status()

        # -- files dropped on the window (gui_files.handle_drop) ------------
        def dragEnterEvent(self, ev):
            from . import gui_files as _gf
            if _gf.drop_paths(ev.mimeData()):
                ev.acceptProposedAction()
            else:
                super().dragEnterEvent(ev)

        def dragMoveEvent(self, ev):
            from . import gui_files as _gf
            if _gf.drop_paths(ev.mimeData()):
                ev.acceptProposedAction()
            else:
                super().dragMoveEvent(ev)

        def dropEvent(self, ev):
            from . import gui_files as _gf
            paths = _gf.drop_paths(ev.mimeData())
            if not paths:
                return super().dropEvent(ev)
            ev.acceptProposedAction()
            pos = self.mapToGlobal(ev.position().toPoint())
            # after the drop returns: a menu or dialog inside a drop handler
            # leaves the drag source waiting
            QtCore.QTimer.singleShot(0, lambda: _gf.handle_drop(self, paths, pos))

        # -- the active tab stands in for the window ----------------------
        def __getattr__(self, name):
            # Only reached when the window itself has no such attribute.
            if name.startswith("__"):
                raise AttributeError(name)
            tabs = self.__dict__.get("tabs")
            ws = tabs.currentWidget() if tabs is not None else None
            if ws is None:
                raise AttributeError(name)
            return getattr(ws, name)

        @property
        def current(self) -> Optional["Workspace"]:
            return self.tabs.currentWidget()

        def workspaces(self):
            return [self.tabs.widget(i) for i in range(self.tabs.count())]

        def show_workspace(self, ws):
            i = self.tabs.indexOf(ws)
            if i >= 0 and i != self.tabs.currentIndex():
                self.tabs.setCurrentIndex(i)

        def find_open(self, path) -> Optional["Workspace"]:
            """The tab whose primary is ``path``, if any."""
            key = _path_key(path)
            if key is None:
                return None
            for ws in self.workspaces():
                if _path_key(ws.source_path()) == key:
                    return ws
            return None

        # -- tabs ---------------------------------------------------------
        def _add_workspace(self, primary=None, comparison=None,
                           activate=True) -> "Workspace":
            ws = Workspace(self, primary, comparison)
            if primary is not None:
                ws._seen_rev = primary.revision
            if comparison is not None:
                ws._seen_cmp_rev = comparison.revision
            i = self.tabs.addTab(ws, ws.tab_label())
            self._workspace_changed(ws)
            if activate:
                self.tabs.setCurrentIndex(i)
            return ws

        def _remove_workspace(self, ws, remember=True):
            """Take a tab away without asking (the caller has asked). A tab
            holding a file goes into the closed-tab history."""
            if remember:
                gone = self.closed_entry(ws)
                if ws.project_ref is not None:
                    # a project goes into the history as a whole, when it
                    # closes (gui_project.note_closing)
                    _gp.note_closing(self, ws, gone)
                elif gone is not None:
                    self.push_closed(gone)
            else:
                _gp.leaving(ws)             # its definition snapshot stays
            ws.shutdown()
            _gp.tab_removed(self, ws)       # a project closes with its last tab
            for other in self.workspaces():
                if other.cmp_link is ws:
                    other.freeze_comparison()
            i = self.tabs.indexOf(ws)
            if self._prev_ws is ws:
                self._prev_ws = None
            if i >= 0:
                self.tabs.removeTab(i)
            ws.deleteLater()
            if self.tabs.count() == 0:
                self._add_workspace()       # the window always has a tab

        def close_tab(self, ws=None) -> bool:
            ws = ws or self.current
            if ws is None or not ws.confirm_close():
                return False
            self._remove_workspace(ws)
            return True

        def _close_tab_at(self, i):
            ws = self.tabs.widget(i)
            if ws is not None:
                self.close_tab(ws)

        def _cycle_tab(self, step):
            n = self.tabs.count()
            if n > 1:
                self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)

        def _on_tab_changed(self, _i):
            prev, ws = self._prev_ws, self.current
            if prev is not None and prev is not ws:
                try:
                    prev.suspend_tools()
                except RuntimeError:
                    pass
            self._prev_ws = ws
            if ws is None:
                return
            ws.poll()                       # catch up on anything missed
            ws.resume_tools()
            self.swap_act.setChecked(swap_axes())
            ws.follow_swap()                # it may have been flipped elsewhere
            self._refresh_enabled()
            self._status()

        def _workspace_changed(self, ws):
            """A tab's file, name, dirty state or comparison changed: redraw
            its tab, and the window title and status line when it is the
            active one."""
            i = self.tabs.indexOf(ws)
            if i < 0:
                return
            from . import gui_start as _gs
            _gs.sync(ws)
            bar = self.tabs.tabBar()
            self.tabs.setTabText(i, ws.tab_label())
            self.tabs.setTabToolTip(i, ws.tab_tooltip())
            bar.setTabData(i, ws.tint())
            bar.update()
            if ws is self.current:
                from . import gui_undomenu as _gum
                _gum.sync(self)             # Undo / Redo follow the history
                if ws.project_ref is not None:
                    self.setWindowTitle(branding.title(_gp.window_title(ws)))
                else:
                    self.setWindowTitle(branding.title(ws.primary.binfile.name)
                                        if ws.primary is not None
                                        else branding.title())
                text = ws.status_text()
                self._status_label.setText(text)
                self._status_label.setToolTip(text)

        def _status(self):
            ws = self.current
            if ws is not None:
                self._workspace_changed(ws)

        def _tick(self):
            for ws in self.workspaces():
                try:
                    ws.poll()
                except Exception:           # one bad tab must not stop the rest
                    import traceback
                    traceback.print_exc()

        # -- opening ------------------------------------------------------
        def open_path(self, path, new_tab=False) -> bool:
            """Open ``path`` as a primary: in the active tab, or with
            ``new_tab`` in a new one (a blank active tab is used either way,
            rather than left beside it). A file already open just brings its
            tab forward -- two tabs editing one file would each overwrite the
            other on save.

            The file pickers' folder is NOT moved here: this also runs for
            Open Recent, Reopen tabs at startup and the tab history, and
            letting those move it sent File > Open back to wherever the
            reopened tabs live rather than where the last file was picked
            (see _ask_path)."""
            if not path:
                return False
            if _gp.parse_ref(path) is None and _gp.is_project(path):
                return _gp.open_project(self, path, new_tab=new_tab)
            there = self.find_open(path)
            if there is not None:
                self.show_workspace(there)
                self.statusBar().showMessage(
                    "%s is already open in this tab." % _os.path.basename(path),
                    5000)
                return True
            cur = self.current
            if not new_tab or cur is None or cur.is_empty():
                return cur._load_primary(path)
            prev = cur
            ws = self._add_workspace()
            if ws._load_primary(path):
                return True
            self._remove_workspace(ws)
            self.show_workspace(prev)
            return False

        def _ask_path(self, title):
            """A binary picked in a file dialog (primary or comparison). Its
            folder is where the next one opens -- only a pick moves it."""
            from .recent import get_last_dir, set_last_dir
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, title, get_last_dir(),
                "All files (*);;OVT projects (*.ovt);;"
                "ECU & flash files (*.bin *.sgo *.dim *.frf);;"
                "WinOLS projects (*.ols);;"
                "Definitions (*.xdf *.a2l *.kp *.dam);;"
                "OVT containers (*.edc16 *.edc15 *.6hp *.8hp *.hvac *.vdo);;"
                "Cluster EEPROM (*.vdo *.bin)")
            if path:
                set_last_dir(path)
            return path

        def open_primary(self):
            path = self._ask_path("Open primary binary")
            if path:
                self.open_path(path)

        def new_tab(self):
            """An empty tab (internal: the window keeps one when nothing is
            open)."""
            return self._add_workspace()

        def _fill_tab_list(self, menu):
            menu.clear()
            for i in range(self.tabs.count()):
                ws = self.tabs.widget(i)
                a = menu.addAction(_swatch(ws.tint()), ws.tab_label())
                a.setToolTip(ws.tab_tooltip())
                a.setCheckable(True)
                a.setChecked(ws is self.current)
                a.triggered.connect(lambda _c=False, w=ws: self.show_workspace(w))

        def _plus_clicked(self):
            """The + beside the tabs: the File > Import menu, under it."""
            b = self._plus_btn
            self._refresh_enabled()
            self.import_menu.popup(b.mapToGlobal(QtCore.QPoint(0, b.height())))

        def open_primary_new_tab(self):
            path = self._ask_path("Open primary binary in a new tab")
            if path:
                self.open_path(path, new_tab=True)

        def open_comparison(self):
            path = self._ask_path("Open comparison binary")
            if path:
                self.open_comparison_path(path)

        def open_comparison_path(self, path) -> bool:
            """Load ``path`` as the active tab's comparison. When that file is
            open in another tab, offer that tab instead -- its unsaved edits
            are what the user is looking at, not the file on disk."""
            cur = self.current
            if cur is None or cur.primary is None:
                return False
            other = self.find_open(path)
            if other is not None and other is not cur:
                SB = QtWidgets.QMessageBox.StandardButton
                box = QtWidgets.QMessageBox(
                    QtWidgets.QMessageBox.Icon.Question, "Open comparison",
                    "%s is open in another tab." % _os.path.basename(path),
                    SB.Cancel, self)
                box.setInformativeText(
                    "Compare against that tab (it follows the tab's edits, "
                    "saved or not), or load the file as it is on disk?")
                live = box.addButton("Use the open tab",
                                     QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                disk = box.addButton("Load from disk",
                                     QtWidgets.QMessageBox.ButtonRole.ActionRole)
                box.setDefaultButton(live)
                box.exec()
                if box.clickedButton() is live:
                    return cur.link_comparison(other)
                if box.clickedButton() is not disk:
                    return False
            return cur._load_comparison(path)

        def adopt_session(self, session, path, view_only=None):
            """Give an already-loaded session a tab of its own (``view_only``:
            a saved version shown read-only -- gui_viewonly)."""
            ws = self._add_workspace()
            if view_only:
                ws._pending_view_only = view_only
            ws.adopt_primary(session, path)
            return ws

        def load_from_image(self, data, name):
            """A read from a module: straight into a project -- the active
            tab's, if the user says so, else a new untitled one -- with the
            read as its original. Never replaces anything already open."""
            return _gp.open_read(self, data, name) is not None

        def load_backup(self, flash, eeprom, path=None, name=None):
            """A flash + EEPROM pair: a full-backup read (into a project, as
            one read), or a .edc15 full backup opened from disk (a tab)."""
            if path is None:
                return _gp.open_read(self, flash, name or "ECU-backup",
                                     eeprom=eeprom) is not None
            if path:
                there = self.find_open(path)
                if there is not None:
                    self.show_workspace(there)
                    return True
            prev = self.current
            ws = self._add_workspace()
            if not ws.load_backup(flash, eeprom, path=path):
                self._remove_workspace(ws)
                return False
            if prev is not None and prev is not ws and prev.is_empty():
                self._remove_workspace(prev)
            return True

        # -- menus of other tabs ------------------------------------------
        def _tab_entry(self, ws):
            badge = platform_badge(ws.platform())[0]
            return "%s  %s" % (badge, ws.display_name())

        def fill_compare_menu(self, menu):
            """Open comparison ▸ from an open tab: every other tab, the ones
            of another platform shown but disabled (they cannot pair)."""
            menu.clear()
            cur = self.current
            others = [w for w in self.workspaces()
                      if w is not cur and w.primary is not None]
            if cur is None or cur.primary is None or not others:
                a = menu.addAction("(no other tab has a file open)")
                a.setEnabled(False)
                return
            for w in others:
                a = menu.addAction(_swatch(w.tint()), self._tab_entry(w))
                a.setToolTip(w.tab_tooltip())
                if w.platform() != cur.platform():
                    a.setText(a.text() + "   — different platform")
                    a.setEnabled(False)
                elif cur.cmp_link is w:
                    a.setCheckable(True)
                    a.setChecked(True)
                a.triggered.connect(
                    lambda _c=False, o=w: self.current.link_comparison(o))

        #: newest saved versions listed straight in the menu; the rest are
        #: one click away in Version History
        VERSION_MENU_MAX = 25

        def fill_version_menu(self, menu):
            """Compare with saved version ▸: the active tab's own save
            history, newest first -- pick one and it becomes the comparison."""
            menu.clear()
            cur = self.current
            if cur is None or cur.primary is None:
                a = menu.addAction("(open a file first)")
                a.setEnabled(False)
                return
            if cur.project_ref is not None:
                return _gp.fill_version_menu(self, menu, cur)
            from . import history as _history
            try:
                versions = _history.list_versions(cur.primary)
            except Exception:
                versions = []
            if not versions:
                a = menu.addAction("(no saved versions yet -- every save adds one)")
                a.setEnabled(False)
                return
            shown = cur.cmp_version["id"] if cur.cmp_version else None
            from . import options as _opt
            cap = _opt.get("history/version_menu_max")   # VERSION_MENU_MAX
            for i, ver in enumerate(versions[:cap]):
                when = ver.get("ts", "").replace("T", " ")[:16]
                name = _history.display_label(ver)
                if len(name) > 70:
                    name = name[:69].rstrip() + "\u2026"
                text = "%s   %s" % (when, name)
                if ver.get("pending"):
                    text += "   (unsaved)"
                elif i == 0:
                    text += "   (last save)"
                a = menu.addAction(text)
                a.setToolTip(_history.display_label(ver))
                if ver["id"] == shown:
                    a.setCheckable(True)
                    a.setChecked(True)
                a.triggered.connect(
                    lambda _c=False, vid=ver["id"]:
                        self.current.compare_with_version(vid))
            menu.addSeparator()
            more = len(versions) - cap
            menu.addAction(
                "Version history...%s" % ("  (%d more)" % more if more > 0 else ""),
                self._on_ws("open_version_history"))

        def fill_copy_from_menu(self, menu, ws=None):
            """Copy values from ▸ tab: every other tab, enabled where the
            parameter on screen exists there with the same shape."""
            ws = ws or self.current
            menu.clear()
            others = [w for w in self.workspaces()
                      if w is not ws and w.primary is not None]
            if ws is None or ws.model.entity is None:
                a = menu.addAction("(select a parameter first)")
                a.setEnabled(False)
                return
            if not others:
                a = menu.addAction("(no other tab has a file open)")
                a.setEnabled(False)
                return
            for w in others:
                src, why = ws.copy_source(w)
                a = menu.addAction(_swatch(w.tint()), self._tab_entry(w))
                a.setToolTip(w.tab_tooltip())
                if src is None:
                    a.setText(a.text() + "   — " + why)
                    a.setEnabled(False)
                a.triggered.connect(
                    lambda _c=False, t=ws, o=w: t.copy_from_tab(o))

        # -- menu / actions -----------------------------------------------
        def _on_ws(self, name, *args):
            """A slot that runs ``name`` on whichever tab is active when the
            action fires (not when the menu was built)."""
            def slot(*_a):
                ws = self.current
                if ws is not None:
                    return getattr(ws, name)(*args)
            return slot

        def _make_actions(self):
            self._needs_file = []          # actions disabled with no file open
            self._conditional = []         # (action, predicate) -- disabled unless predicate() too
            cw = lambda: self.current      # noqa: E731
            has_cmp = lambda: cw().comparison is not None   # noqa: E731
            bar = self.menuBar()
            m = bar.addMenu("&File")
            from . import gui_files as _gf
            self._act(m, "New project...", lambda: _gp.new_project(self),
                      "Ctrl+N").setToolTip(
                "Start a project: describe the vehicle and pick the files it "
                "begins with")
            self._act(m, "Open project...", lambda: _gf.open_project_dialog(self),
                      "Ctrl+O")
            self.recent_menu = m.addMenu("Open &Recent")
            self.recent_menu.aboutToShow.connect(self._rebuild_recent)
            self.project_open_menu = m.addMenu("Open from &project")
            self.project_open_menu.aboutToShow.connect(
                lambda: _gp.fill_open_menu(self, self.project_open_menu))
            self.history_menu = m.addMenu("Restore project from &history")
            self.history_menu.aboutToShow.connect(
                lambda: self.fill_history_menu(self.history_menu))
            self._act(m, "Reopen last closed project", self.reopen_last_closed,
                      "Ctrl+Shift+T")
            m.addSeparator()
            imp = self.import_menu = m.addMenu("&Import")
            in_project = lambda: (self.current is not None and   # noqa: E731
                                  self.current.project_ref is not None)
            has_tab = lambda: (self.current is not None and   # noqa: E731
                               not self.current.is_empty())
            self._act(imp, "Into a new project...",
                      lambda: _gf.import_dialog(self, "new"), "Ctrl+Shift+I"
                      ).setToolTip(
                "Binaries, containers (.sgo, .frf, .dim ...), WinOLS projects: "
                "each binary opens in a new tab")
            self._imp_cur = self._act(
                imp, "Into this project...",
                lambda: _gf.import_dialog(self, "current"),
                enabled_when=in_project)
            imp.addSeparator()
            self._act(imp, "Into a new project, in this tab...",
                      lambda: _gf.import_dialog(self, "new", in_tab=True),
                      enabled_when=has_tab).setToolTip(
                "Closes this tab first (asking about unsaved changes)")
            self._act(imp, "Into this project, in this tab...",
                      lambda: _gf.import_dialog(self, "current", in_tab=True),
                      enabled_when=in_project).setToolTip(
                "Closes this tab first (asking about unsaved changes)")
            imp.addSeparator()
            self._act(imp, "Definition file for this binary (XDF, A2L, KP, OLS, "
                      "DAM)...", lambda: _gf.import_definition_dialog(self),
                      enabled_when=in_project).setToolTip(
                "A third-party definition, packaged in the project with this "
                "binary: its maps open in the main view")
            m.addSeparator()
            self._act(m, "Import as comparison...",
                      lambda: _gf.import_comparison_dialog(self),
                      needs_file=True).setToolTip(
                "A binary, or a version of a binary in a project")
            self.recent_cmp_menu = m.addMenu("Import recent as &comparison")
            self.recent_cmp_menu.aboutToShow.connect(
                self._rebuild_recent_comparisons)
            self._needs_file.append(self.recent_cmp_menu.menuAction())
            self.compare_tab_menu = m.addMenu("Compare with open &tab")
            self.compare_tab_menu.aboutToShow.connect(
                lambda: self.fill_compare_menu(self.compare_tab_menu))
            self._needs_file.append(self.compare_tab_menu.menuAction())
            self.compare_version_menu = m.addMenu("Compare with saved &version")
            self.compare_version_menu.aboutToShow.connect(
                lambda: self.fill_version_menu(self.compare_version_menu))
            self._needs_file.append(self.compare_version_menu.menuAction())
            m.addSeparator()
            self._act(m, "File information...",
                      self._on_ws("show_file_info"), "Ctrl+I",
                      needs_file=True)
            self._act(m, "Project information...",
                      lambda: _gp.edit_current_info(self), needs_file=True,
                      enabled_when=lambda: cw().project_ref is not None
                      ).setToolTip("The vehicle this project is for: make, "
                                   "model, chassis, model year, VIN ...")
            self.cmpinfo_act = self._act(
                m, "Comparison information...",
                lambda: cw().show_file_info(cw().comparison),
                needs_file=True, enabled_when=has_cmp)
            m.addSeparator()
            self.close_tab_act = self._act(m, "Close tab", self.close_tab,
                                           "Ctrl+W")
            self.close_cmp_act = self._act(
                m, "Close comparison", self._on_ws("close_comparison"),
                needs_file=True, enabled_when=has_cmp)
            m.addSeparator()
            self._act(m, "Save", self._on_ws("save"), "Ctrl+S", needs_file=True)
            self._act(m, "Save this branch only", self._on_ws("save_tab_only"),
                      "Ctrl+Alt+S", needs_file=True, enabled_when=in_project
                      ).setToolTip("Save only this tab's branch into its "
                                   "project, leaving the project's other tabs "
                                   "unsaved")
            self._act(m, "Save As...", self._on_ws("save_as"), needs_file=True)
            self._act(m, "Close project",
                      lambda: _gp.close_project(self, cw().project_ref.doc),
                      needs_file=True, enabled_when=in_project)
            # every export in one place, all made from the project (its last
            # saved version -- offered to save first); items that do not
            # apply stay listed but disabled, so the menu keeps its shape
            x = self.export_menu = m.addMenu("&Export")
            self._needs_file.append(x.menuAction())
            in_proj = lambda: cw().project_ref is not None   # noqa: E731
            self._act(x, "Binary (.bin)...", lambda: _gp.export_raw(cw()),
                      needs_file=True).setToolTip(
                "A plain image of one part of the set -- flash, MPC or EEPROM "
                "-- as last saved")
            self._act(x, "Original file (.sgo / .dim / .frf)...",
                      lambda: _gp.export_original(cw()), needs_file=True,
                      enabled_when=lambda: (in_proj()
                                            and bool(cw().project_ref.set.originals))
                      ).setToolTip("The file the set was imported from, with "
                                   "the saved version in it (an edited .frf "
                                   "cannot be rebuilt yet)")
            x.addSeparator()
            self._act(x, "Definition as XDF...", self._on_ws("export_xdf"),
                      needs_file=True,
                      enabled_when=lambda: getattr(cw().primary, "support",
                                                   "full") == "full")
            from . import gui_defexport as _gde     # A2L / WinOLS map pack
            for text, fn in (("Definition as A2L...", _gde.export_a2l),
                             ("Map pack for WinOLS (.kp, experimental)...",
                              _gde.export_kp)):
                self._act(x, text, lambda _c=False, f=fn: f(cw()),
                          needs_file=True,
                          enabled_when=lambda: getattr(cw().primary, "support",
                                                       "full") == "full")
            x.addSeparator()
            for kind, text in (("log", "Data log..."), ("patch", "Patch..."),
                               ("attachment", "Attachment...")):
                self._act(x, text,
                          lambda _c=False, k=kind: _gp.export_kept(self, k),
                          needs_file=True,
                          enabled_when=lambda k=kind: (
                              in_proj() and bool(_gp.kept(cw().project_ref.doc, k))))
            m.addSeparator()
            self._act(m, "New window", self.new_window).setToolTip(
                "Start another %s in a window of its own (a project open in "
                "one window can't be opened in the other)" % branding.APP_NAME)
            self._act(m, "Exit", self.close)
            e = bar.addMenu("&Edit")
            from . import gui_undomenu as _gum  # toolbar history drop-downs
            _gum.register(
                self,
                self._act(e, "Undo", self._on_ws("undo"), "Ctrl+Z",
                          needs_file=True),
                self._act(e, "Redo", self._on_ws("redo"), "Ctrl+Y",
                          needs_file=True))
            e.addSeparator()
            self._act(e, "Find...", self.search_dock.open, "Ctrl+F")
            # the toolbar's Find button shows / hides the panel
            from . import gui_actions
            gui_actions.add_extra(self, "edit/find-panel",
                                  self.search_dock.toggle_action(),
                                  "Edit ▸ Find panel (show / hide)")
            self._act(e, "Find in definition...",
                      self.search_dock.open_in_definition, "Ctrl+Shift+F",
                      needs_file=True,
                      enabled_when=lambda: bool(getattr(
                          cw().primary, "has_definition_file", False))
                      ).setToolTip(
                "Search only the maps of the tab's definition file (an "
                "imported WinOLS project, an attached XDF / A2L / DAMOS)")
            self._act(e, "Find next result", lambda: self.search_dock.step(1),
                      "F3")
            self._act(e, "Find previous result",
                      lambda: self.search_dock.step(-1), "Shift+F3")
            e.addSeparator()
            self._act(e, "Scale selection...", self._on_ws("scale"), needs_file=True)
            e.addSeparator()
            self._act(e, "Copy cells", self._on_ws("_copy_cells"), "Ctrl+C",
                      needs_file=True)
            self._act(e, "Paste cells", self._on_ws("_paste_cells"), "Ctrl+V",
                      needs_file=True)
            self.copy_tab_menu = e.addMenu("Copy values from tab")
            self.copy_tab_menu.aboutToShow.connect(
                lambda: self.fill_copy_from_menu(self.copy_tab_menu))
            self._needs_file.append(self.copy_tab_menu.menuAction())
            from . import gui_cellkeys as _gck      # Edit > Cells (keyboard)
            _gck.add_menu(self, e)
            e.addSeparator()
            self.diff_act = self._act(e, "Show differences...",
                                      self._on_ws("show_differences"),
                                      needs_file=True, enabled_when=has_cmp)
            e.addSeparator()
            self.patch_gen_act = self._act(
                e, "Generate patch...", self._on_ws("open_patch_builder"),
                needs_file=True, enabled_when=has_cmp)
            self._act(e, "Apply patch...", self._on_ws("open_patch_apply"),
                      needs_file=True)
            self._act(e, "Remove patch...", self._on_ws("open_patch_remove"),
                      needs_file=True)
            e.addSeparator()
            self._act(e, "Version history...", self._on_ws("open_version_history"),
                      needs_file=True)
            self._act(e, "Generate patch from history...",
                      self._on_ws("open_history_patch"), needs_file=True)
            view = bar.addMenu("&View")
            self.swap_act = self._act(
                view, "Swap X and Y axes", self._on_ws("_toggle_transpose"),
                "Ctrl+T", needs_file=True)
            self.swap_act.setCheckable(True)
            self.swap_act.setChecked(swap_axes())
            self.swap_act.setToolTip(
                "Turn every table the other way round (1-D tables down "
                "instead of across). Display only -- the values keep the "
                "cells they are stored in.")
            view.addSeparator()
            self._act(view, "Keep table open", self._on_ws("pin_table"),
                      needs_file=True).setToolTip(
                "Turn the italic preview tab into one that stays open "
                "(double-click a table or its tab does the same)")
            self._act(view, "Close table", self._on_ws("close_table"),
                      "Ctrl+F4", needs_file=True)
            self._act(view, "Next table", self._on_ws("cycle_table", 1),
                      "Ctrl+PgDown", needs_file=True)
            self._act(view, "Previous table", self._on_ws("cycle_table", -1),
                      "Ctrl+PgUp", needs_file=True)
            view.addSeparator()
            self._act(view, "Project panel", lambda: _gp.toggle_panel(self),
                      "Ctrl+Shift+P").setToolTip(
                "Show the Project panel expanded, or collapse it to its strip")
            from . import gui_panelayout as _gpl
            _gpl.add_menu(self, view)
            view.addSeparator()
            self._act(view, "Next tab", lambda: self._cycle_tab(1), "Ctrl+Tab")
            self._act(view, "Previous tab", lambda: self._cycle_tab(-1),
                      "Ctrl+Shift+Tab")
            tools = self.tools_menu = bar.addMenu("&Tools")
            # Tools that read the parameter tree need the platform's module
            # (and a definition for this binary); the hex editor, checksums,
            # flasher, logger, sniffer and simulator never do.
            full = lambda: getattr(cw().primary, "support", "full") == "full"  # noqa: E731
            self._act(tools, "DTC / Fault-Path editor...", self._on_ws("open_dtc"),
                      "Ctrl+D", needs_file=True, enabled_when=full)
            def _msb_ok():
                # only binaries that carry measuring-block definitions (EDC15
                # xcwK / EDC16 group map); a ZF, Aisin or VDO file has none
                from .msblock import has_measuring_blocks
                c = cw()
                return full() and has_measuring_blocks(getattr(c, "primary", None))

            self._act(tools, "Measuring blocks...", self._on_ws("open_msblock"),
                      "Ctrl+M", needs_file=True, enabled_when=_msb_ok)

            def _eoi_open(_c=False):
                from . import gui_eoi as _ge   # End of injection (eoi.py)
                if cw() is not None:
                    _ge.open_for(cw())

            def _eoi_ok():
                try:
                    from . import gui_eoi as _ge
                    return _ge.availability(cw()) is None
                except Exception:
                    return False
            # with the other tools that read the parameter tree
            self._act(tools, "End of injection...", _eoi_open,
                      needs_file=True, enabled_when=_eoi_ok)

            def _dyno_open(_c=False):
                from . import gui_dyno as _gdy  # calculated dyno chart (dyno.py)
                if cw() is not None:
                    _gdy.open_for(cw())

            def _dyno_ok():
                try:
                    from . import gui_dyno as _gdy
                    return _gdy.availability(cw()) is None
                except Exception:
                    return False
            self._act(tools, "Dyno chart...", _dyno_open,
                      needs_file=True, enabled_when=_dyno_ok)

            def _fsw_open(_c=False):
                from . import gui_featureswitch as _gfs   # feature switches
                if cw() is not None:
                    _gfs.open_for(cw())

            def _fsw_ok():
                try:
                    from . import gui_featureswitch as _gfs
                    return _gfs.availability(cw()) is None
                except Exception:
                    return False
            self._act(tools, "Feature switches...", _fsw_open,
                      needs_file=True, enabled_when=_fsw_ok)
            # an engine ECU's (or a cluster's) settings: not on a gearbox tab
            from . import toolrel as _tr
            self._act(tools, "Gear ratios...", self._on_ws("open_gears"), "Ctrl+G",
                      needs_file=True,
                      enabled_when=lambda: full() and _tr.has_gear_ratios(cw().primary))
            self._act(tools, "VSS pulse settings...", self._on_ws("open_vss"),
                      "Ctrl+U", needs_file=True,
                      enabled_when=lambda: full() and _tr.has_vss(cw().primary))
            self._act(tools, "Hex editor...", self._on_ws("open_hex"), "Ctrl+H",
                      needs_file=True)
            self._act(tools, "Verify checksums...", self._on_ws("verify_checksums"),
                      needs_file=True)
            self._ee_act = self._act(tools, "EEPROM editor...",
                                     self._on_ws("open_eeprom_editor"))
            self._sync_eeprom_act()
            tools.addSeparator()
            self._act(tools, "ECU read / write...", lambda: self.open_flash("read"),
                      "Ctrl+E")
            self._act(tools, "Data logging...", self.open_datalog, "Ctrl+L")
            def _logtune_ok():
                # EDC16: the engine analysis. Any calibration with the
                # KF_MEHAUPT maps (ZF 6HP): the shift torque report.
                p = cw().primary
                if getattr(p, "platform", "") == "edc16" and full():
                    return True
                try:
                    from .logtune.shifttrq import available
                except Exception:
                    return False
                return available(p)
            self._act(tools, "Tune from logs...", self._on_ws("open_logtune"),
                      needs_file=True, enabled_when=_logtune_ok)
            self._act(tools, "Bus sniffer (J2534)...", self.open_sniffer)
            self._act(tools, "ECU simulator (bench)...", self.open_simulator)
            self._act(tools, "Converter (definitions, values and binaries)...",
                      self.open_converter)
            tools.addSeparator()
            # tools of one module only: a sub-menu per module
            def _mod_menu(mid, fallback):
                spec = _registry.installed().get(mid)
                sub = tools.addMenu((spec.name if spec else fallback))
                if spec is None:
                    sub.menuAction().setEnabled(False)
                    sub.menuAction().setToolTip(
                        _registry.missing_text(mid, "these tools"))
                return sub
            e16 = self.edc16_menu = _mod_menu("edc16", "Bosch EDC16")

            def _builder():
                import importlib            # the EDC16 module's own window
                importlib.import_module(".edc16.gui_builder16",
                                        __package__).open_builder(self)
            self._act(e16, "Block builder...", lambda _c=False: _builder()
                      ).setToolTip(
                "Put one EDC16U31/U34 together from blocks of different files "
                "(boot, program, calibrations, MPC, EEPROM), checked against "
                "each other")
            zf = self.zf6hp_menu = _mod_menu("zf6hp", "ZF 6HP")
            self.program_area_act = self._act(
                zf, "Correct program-area checksums",
                lambda checked: self._toggle_program_area(checked),
                needs_file=True,
                enabled_when=lambda: getattr(cw().primary, "platform", "")
                == "zf6hp" and _registry.can("zf6hp", "checksum"))
            self.program_area_act.setCheckable(True)
            self.program_area_act.setChecked(program_area_pref())
            self.program_area_act.setToolTip(
                "Also rewrite the ZF 6HP program/boot-area descriptor self-CRCs "
                "(zlib CRC-32) on save/flash, not just the calibration checksum. "
                "Off by default -- a calibration tune never changes them; turn "
                "it on only when editing program/boot bytes or a descriptor's "
                "identity.")
            def _shiftedit_ok(kind="shift", platform="zf6hp"):
                try:
                    from .gui_gearbox_edit import available
                except Exception:
                    return False
                p = cw().primary
                return p is not None and getattr(p, "platform", "") == platform \
                    and available(p, kind) and not getattr(cw(), "view_only", None)
            self._act(zf, "Shift map editor...", self._on_ws("open_shift_editor"),
                      needs_file=True, enabled_when=_shiftedit_ok).setToolTip(
                "Edit many shift schedules at once: offset, scale or slide a "
                "shift line across a selection, with a live preview and "
                "advisory checks; applies as one undoable step.")
            self._act(zf, "Lock-up (TCC) editor...", self._on_ws("open_tcc_editor"),
                      needs_file=True, enabled_when=lambda: _shiftedit_ok("tcc")).setToolTip(
                "Edit the converter lock-up characteristics by gear: RO / OR / RC "
                "thresholds across every characteristic a gear uses, following "
                "the upshift if wanted, with a preview; one undoable step.")
            z8 = self.zf8hp_menu = _mod_menu("zf8hp", "ZF 8HP")
            self._act(z8, "Shift map editor...", self._on_ws("open_shift_editor"),
                      needs_file=True, enabled_when=lambda: _shiftedit_ok("shift", "zf8hp")
                      ).setToolTip(
                "Edit many 8HP shift programs at once (14 lines, 1>2..8>7): offset, "
                "scale or slide a line across a selection, with a live preview and "
                "advisory checks; applies as one undoable step.")
            self._act(z8, "Lock-up (TCC) editor...", self._on_ws("open_tcc_editor"),
                      needs_file=True, enabled_when=lambda: _shiftedit_ok("tcc", "zf8hp")
                      ).setToolTip(
                "Edit the 8HP converter-clutch (WK) characteristics by the gear "
                "WT_KWK_ZTAB gives them: RO / OR / RC / COC thresholds, with a "
                "preview; one undoable step.")
            vdo = self.vdo_menu = _mod_menu("vdo", "VDO Cluster")
            self._act(vdo, "Needle sweep...", self._on_ws("open_sweep"),
                      needs_file=True,
                      enabled_when=lambda: getattr(cw().primary, "platform", "")
                      == "vdo" and full()).setToolTip(
                "Apply a needle / LED sweep or DSG-needle patch to a VDO "
                "cluster EEPROM (only patches for its exact ROM build)")
            tools.addSeparator()
            _gui_ai.add_menu(self, tools)
            self._act(tools, "Modules...", self.open_modules).setToolTip(
                "Which control-module modules are installed; install or remove one")
            from . import gui_toolsmenu as _gtm
            _gtm.install(self)              # only the tools the tab can use
            helpm = bar.addMenu("&Help")
            # F1 itself is handled app-wide (gui_help.install_f1): help for
            # the focused panel or the window in front; "\tF1" only shows the key here
            self._act(helpm, "Help contents\tF1",
                      lambda: self.open_help()).setToolTip(
                "The help pages for the app and every installed module")
            self._act(helpm, "Check for Updates...", self.check_updates).setToolTip(
                "Look for a newer %s and newer versions of your modules"
                % branding.APP_NAME)
            helpm.addSeparator()
            self._act(helpm, "About %s..." % branding.APP_NAME,
                      lambda: self.open_about(0)).setMenuRole(
                QtGui.QAction.MenuRole.AboutRole)
            self._act(helpm, "Credits && licences...", lambda: self.open_about(1))
            helpm.addSeparator()
            self._act(helpm, "About Qt...",
                      lambda: QtWidgets.QMessageBox.aboutQt(self)).setMenuRole(
                QtGui.QAction.MenuRole.AboutQtRole)
            self._rebuild_recent()
            self._rebuild_recent_comparisons()

        def _act(self, menu, text, slot, shortcut=None, needs_file=False,
                 enabled_when=None):
            a = QtGui.QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            menu.addAction(a)
            self.addAction(a)
            if needs_file:
                self._needs_file.append(a)
            if enabled_when is not None:
                self._conditional.append((a, enabled_when))
            return a

        def open_help(self, topic=None):
            """Help > Help contents (or the page ``topic``)."""
            from .gui_help import show_help
            show_help(self, topic)

        def check_updates(self):
            """Help > Check for Updates... (gui_update)."""
            from .gui_update import check_now
            check_now(self)

        def open_about(self, tab=0):
            """Help > About (tab 0) or Credits & licences (tab 1)."""
            from .gui_about import show_about
            show_about(self, tab)

        def open_modules(self):
            from .gui_modules import ModuleManagerDialog
            ModuleManagerDialog(self).exec()
            self._sync_eeprom_act()

        def _sync_eeprom_act(self):
            """Tools > EEPROM editor: an ECU EEPROM layout comes from a
            module (EDC15, EDC16); without one the item says which."""
            from . import gui_eeprom
            ok = gui_eeprom.available()
            self._ee_act.setEnabled(ok)
            self._ee_act.setToolTip(
                "The EEPROM of the tab's control unit (EDC15, EDC16), or an "
                "EEPROM image from a file" if ok else
                "Needs the Bosch EDC15 or EDC16 module (Tools > Modules...)")

        def _toggle_program_area(self, checked):
            # a global preference: every open tab follows it, as a file
            # opened later does (it reads the preference at load)
            set_program_area_pref(bool(checked))
            for ws in self.workspaces():
                if getattr(ws, "primary", None) is not None:
                    try:
                        ws.primary.correct_program_area = bool(checked)
                    except Exception:
                        pass

        def _refresh_enabled(self):
            ws = self.current
            has = ws is not None and ws.primary is not None
            for a in getattr(self, "_needs_file", []):
                a.setEnabled(has)
            for a, check in getattr(self, "_conditional", []):
                try:
                    ok = has and bool(check())
                except Exception:
                    ok = False
                a.setEnabled(ok)
            from . import gui_viewonly as _vo
            _vo.restrict_actions(self, ws)
            from . import gui_undomenu as _gum
            _gum.sync(self)

        def _rebuild_recent(self):
            from . import gui_files as _gf
            _gf.fill_recent_menu(self, self.recent_menu)

        def _rebuild_recent_comparisons(self):
            from . import gui_files as _gf
            _gf.fill_recent_comparisons(self, self.recent_cmp_menu)

        def new_window(self):
            """File > New window: another instance, in a window of its own."""
            import sys
            if getattr(sys, "frozen", False):
                prog, args = sys.executable, ["gui", "--new-instance"]
            else:
                prog, args = sys.executable, ["-m", "ovtsuite", "gui",
                                              "--new-instance"]
            QtCore.QProcess.startDetached(prog, args)

        # -- window-wide tools --------------------------------------------
        def open_datalog(self):
            try:
                from .datalog.gui_log import DataLogDialog
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Data logging", str(ex))
                return
            dlg = getattr(self, "_datalog_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return
            self._datalog_dlg = DataLogDialog(self)
            self._datalog_dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            self._datalog_dlg.show()

        def open_converter(self):
            """Tools > Converter...: definitions and binaries from one format
            to another (ovtsuite.gui_convert)."""
            from .gui_convert import open_in_app
            open_in_app(self)

        def open_sniffer(self):
            try:
                from .sniff.gui_sniff import SnifferDialog
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Bus sniffer", str(ex))
                return
            dlg = getattr(self, "_sniff_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return
            self._sniff_dlg = SnifferDialog(self)
            self._sniff_dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            self._sniff_dlg.show()

        def open_simulator(self):
            try:
                from .flash.gui_sim import SimulatorDialog
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "ECU simulator", str(ex))
                return
            dlg = getattr(self, "_sim_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return
            self._sim_dlg = SimulatorDialog(self)
            self._sim_dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            self._sim_dlg.show()

        def open_flash(self, mode):
            """Read or write a module. Writing sends the ACTIVE tab's primary;
            a read opens in a new tab (see load_from_image)."""
            try:
                from .flash.gui_flash import FlashDialog
            except Exception as ex:
                QtWidgets.QMessageBox.critical(
                    self, "Flashing",
                    "The flashing module could not be loaded (%s). It needs the "
                    "'pyserial' package." % ex)
                return
            # what a write sends is the tab in front now; it is the one marked
            # as flashed in its project when the write succeeds
            self._flash_source = self.current
            dlg = FlashDialog(self, mode)
            if mode == "eeprom":
                dlg.ecu_box.setCurrentIndex(1)
            try:
                dlg.exec()
            finally:
                self._status()

        def before_flash_write(self) -> bool:
            """FlashDialog, before a write: unsaved edits in a project tab may
            be saved first (gui_project.before_flash_write)."""
            return _gp.before_flash_write(
                self, self.__dict__.get("_flash_source") or self.current)

        def flash_written(self, what=""):
            """FlashDialog, after a successful write: mark the version."""
            _gp.mark_flashed(
                self, self.__dict__.get("_flash_source") or self.current, what)

        def keep_attachment(self, filename, data, what) -> bool:
            """FlashDialog: a dump that is not a binary to edit (the C167
            internal ROM) -- kept with the project in front when the user
            says so. False = save it as a file instead."""
            return _gp.keep_attachment(self, filename, data, what)

        def load_eeprom_read(self, data, name):
            """FlashDialog: an EEPROM read, into a project."""
            return _gp.open_eeprom_read(self, data, name) is not None

        # -- closing and reopening ----------------------------------------
        def close_discarding(self):
            """Close without asking about unsaved changes and without
            remembering the tabs -- for tests and scripted use."""
            self._quiet_close = True
            return self.close()

        def closeEvent(self, ev):
            from . import gui_saving as _sv
            if _sv.saving(self):
                ev.ignore()                 # a project is being written
                return
            _gp.pause_autosave(self)        # no snapshot starts mid-close
            if not self._quiet_close:
                dirty = [ws for ws in self.workspaces() if ws.is_dirty()]
                dirty += _gp.unsaved_entries(self)  # project-only changes
                if dirty:
                    dlg = UnsavedDialog(dirty, self)
                    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                        _gp.pause_autosave(self, False)
                        ev.ignore()
                        return
                    if dlg.choice == "save":
                        projects, tabs = dlg.plan()
                        for doc, ptabs in projects:
                            # one write per project, with only the ticked
                            # tabs committed (the others stay unsaved)
                            if not _gp.save_selection(self, doc, ptabs):
                                _gp.pause_autosave(self, False)
                                ev.ignore()     # cancelled a Save As, or failed
                                return
                        for ws in tabs:
                            self.show_workspace(ws)
                            if not ws.save_checked(tab_only=True):
                                _gp.pause_autosave(self, False)
                                ev.ignore()
                                return
                self.remember_tabs()
            # a proper exit: saved or refused, crash-recovery data goes
            _gp.shutdown(self)
            self._watch.stop()
            for ws in self.workspaces():
                ws.shutdown()               # not history: that is last_session
            for name in ("_datalog_dlg", "_sniff_dlg"):
                dlg = self.__dict__.get(name)
                if dlg is not None:
                    try:
                        dlg.close()
                    except RuntimeError:
                        pass
            super().closeEvent(ev)

        def tab_session(self):
            """The open tabs, as :data:`TABS_KEY` stores them."""
            spaces = [w for w in self.workspaces() if w.primary is not None]
            out = []
            for ws in spaces:
                link = ws.cmp_link
                ent = ws.model.entity
                out.append({
                    "primary": ws.source_path(),
                    "platform": ws.platform(),
                    "label": ws.tab_label().replace("*", ""),
                    "comparison": ws.comparison_path(),
                    "cmp_version": ws.cmp_version["id"] if ws.cmp_version else None,
                    "link": spaces.index(link) if link in spaces else None,
                    "entity": (getattr(ent, "name", None) or None)
                    if ent is not None else None,
                    **ws.tables_state(),
                    **_gp.session_project(ws),
                    "active": ws is self.current,
                })
            return out

        def remember_tabs(self):
            import json
            try:
                _settings().setValue(TABS_KEY, json.dumps(self.tab_session()))
            except Exception:
                pass

        @staticmethod
        def remembered_tabs():
            import json
            try:
                raw = _settings().value(TABS_KEY, "")
                data = json.loads(raw) if raw else []
                return [d for d in data if isinstance(d, dict)]
            except Exception:
                return []

        # -- closed-tab history --------------------------------------------
        def closed_entry(self, ws):
            """What it takes to bring ``ws`` back, or None for a tab with
            nothing on disk (empty, or a read never saved)."""
            path = ws.source_path() if ws is not None else None
            if not path:
                return None
            link = ws.cmp_link
            ent = ws.model.entity
            import datetime
            return {
                "primary": path,
                "platform": ws.platform(),
                "label": ws.tab_label().replace("*", "").replace(
                    " (closed tab)", ""),
                "comparison": ws.comparison_path(),
                "cmp_version": ws.cmp_version["id"] if ws.cmp_version else None,
                "link_path": link.source_path() if link is not None else None,
                "entity": (getattr(ent, "name", None) or None)
                if ent is not None else None,
                **ws.tables_state(),
                "closed": datetime.datetime.now().isoformat(timespec="minutes"),
            }

        def closed_history(self):
            import json
            try:
                raw = _settings().value(CLOSED_KEY, "")
                data = json.loads(raw) if raw else []
                return [d for d in data if isinstance(d, dict)
                        and d.get("primary")]
            except Exception:
                return []

        def _save_closed(self, items):
            import json
            try:
                _settings().setValue(CLOSED_KEY,
                                     json.dumps(items[:_closed_max()]))
            except Exception:
                pass

        def push_closed(self, entry):
            """Newest first; one entry per file (closing it again moves it to
            the top rather than listing it twice)."""
            key = _path_key(entry["primary"])
            items = [d for d in self.closed_history()
                     if _path_key(d["primary"]) != key]
            self._save_closed([entry] + items)

        def clear_closed(self):
            self._save_closed([])

        def fill_history_menu(self, menu):
            """File > Restore project from history: recently closed projects
            (and any loose file tab), newest
            first, with when each was closed."""
            menu.clear()
            items = self.closed_history()
            if not items:
                a = menu.addAction("(nothing closed yet)")
                a.setEnabled(False)
                return
            for i, ent in enumerate(items):
                ok = _gp.exists(ent["primary"])
                when = self._when(ent.get("closed"))
                label = ent.get("label") or _os.path.basename(ent["primary"])
                if ent.get("kind") == "project":
                    if ent.get("vehicle") and ent["vehicle"] != label:
                        label += "  --  %s" % ent["vehicle"]
                    tabs = [t.get("set_branch") for t in ent.get("tabs", [])
                            if t.get("set_branch")]
                    if tabs:
                        label += "   (%s)" % ", ".join(tabs)
                col = platform_badge(ent.get("platform"))[2] if ok else None
                a = menu.addAction(_swatch(col), "%s\t%s" % (
                    label if ok else label + "   (file not found)", when))
                tip = [_gp.real_path(ent["primary"])]
                if ent.get("link_path") or ent.get("comparison"):
                    tip.append("Comparison: %s" % (ent.get("link_path")
                                                   or ent.get("comparison")))
                if ent.get("entity"):
                    tip.append("Showing: %s" % ent["entity"])
                a.setToolTip("\n".join(tip))
                a.setEnabled(ok)
                a.triggered.connect(
                    lambda _c=False, e=ent: self.restore_closed(e))
            menu.addSeparator()
            clr = menu.addAction("Clear history")
            clr.triggered.connect(self.clear_closed)

        @staticmethod
        def _when(stamp):
            """"14:05" for today, "Sep 28 14:05" before that."""
            import datetime
            try:
                t = datetime.datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                return ""
            if t.date() == datetime.date.today():
                return t.strftime("%H:%M")
            return t.strftime("%b %d %H:%M")

        def reopen_last_closed(self):
            for ent in self.closed_history():
                if _gp.exists(ent["primary"]) \
                        and self.find_open(ent["primary"]) is None \
                        and _gp._find_doc(self, ent["primary"]) is None:
                    return self.restore_closed(ent)
            self.statusBar().showMessage("No closed project to reopen.", 4000)
            return None

        def _restore_closed_project(self, ent):
            """A project from the history: the tabs it had when it closed,
            with their comparisons and tables (or, without them, the way it
            opens anyway)."""
            path = ent["primary"]
            key = _path_key(path)
            self._save_closed([d for d in self.closed_history()
                               if _path_key(d["primary"]) != key])
            doc = _gp._find_doc(self, path)
            if doc is not None and doc.tabs(self):
                self.show_workspace(doc.tabs(self)[0])
                return doc.tabs(self)[0]
            tabs = [t for t in ent.get("tabs", []) if t.get("primary")]
            made = self.restore_tabs(tabs, list(range(len(tabs)))) if tabs \
                else []
            if not made:
                if not _gp.open_project(self, path):
                    return None
                return self.current
            return made[0]

        def restore_closed(self, ent):
            """Bring a closed tab back in a tab of its own: its primary, its
            comparison (re-linked to the tab that holds that file when one
            is open, else read from disk) and the parameter it showed."""
            path = ent.get("primary")
            if not path or not _gp.exists(path):
                QtWidgets.QMessageBox.information(
                    self, "Restore", "%s is no longer on disk."
                    % (path or "That tab's file"))
                return None
            if ent.get("kind") == "project":
                return self._restore_closed_project(ent)
            there = self.find_open(path)
            if there is not None:
                self.show_workspace(there)
                return there
            if not self.open_path(path, new_tab=True):
                return None
            ws = self.current
            key = _path_key(path)
            self._save_closed([d for d in self.closed_history()
                               if _path_key(d["primary"]) != key])
            other = None
            if ent.get("link_path"):
                other = self.find_open(ent["link_path"])
            ws._quiet = True                # no "comparison loaded" popup
            try:
                if other is not None and other is not ws:
                    ws.link_comparison(other)
                elif ent.get("cmp_version"):
                    ws.compare_with_version(ent["cmp_version"], quiet=True)
                else:
                    cmp_path = ent.get("link_path") or ent.get("comparison")
                    if cmp_path and _os.path.exists(cmp_path) \
                            and ws.comparison is None:
                        ws._load_comparison(cmp_path)
            finally:
                ws._quiet = False
            self._restore_tables_of(ws, ent)
            return ws

        @staticmethod
        def _restore_tables_of(ws, ent):
            """Put back the table tabs a tab history entry lists -- or, from
            before there were table tabs, the one parameter it showed."""
            if ws.primary is None:
                return
            if ent.get("tables"):
                ws.restore_tables(ent["tables"], ent.get("table_active"))
                return
            hit = ws._find_entity(ent.get("entity"))
            if hit is not None:
                ws.show_entity(hit)

        def offer_restore(self):
            """Startup: ask which of last session's projects to reopen (each
            with the tabs it had)."""
            got = self.ask_restore(self)
            if got:
                self.restore_tabs(*got)

        @staticmethod
        def ask_restore(parent=None, place=None):
            """Which of last session's projects to reopen -- asked before the
            main window exists, so the splash runs once, start to finish.
            ``(entries, chosen)`` for :meth:`restore_tabs`, or None.
            ``place(dialog)`` positions the question (the start-up screen)."""
            entries = MainWindow.remembered_tabs()
            if not entries:
                return None
            from . import options as _opt
            mode = _opt.get("startup/restore")     # Options > General
            if mode == "never":
                return None
            if mode == "always":
                return entries, list(range(len(entries)))
            dlg = RestoreProjectsDialog(entries, parent)
            if place is not None:
                place(dlg)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return None
            chosen = dlg.chosen()
            return (entries, chosen) if chosen else None

        def restore_tabs(self, entries, chosen):
            """Reopen ``entries[i]`` for each i in ``chosen``, then put back
            their comparisons (live links where both tabs came back) and the
            parameter each had on screen."""
            made = {}
            from . import gui_splash as _splash
            for k, i in enumerate(chosen):
                ent = entries[i]
                _splash.step("Reopening %s (%d of %d)..." % (
                    ent.get("project_title") or ent.get("label") or "tabs",
                    k + 1, len(chosen)), 0.55 + 0.45 * k / max(1, len(chosen)))
                ent = entries[i]
                path = ent.get("primary")
                if not path or not _gp.exists(path):
                    continue
                cur = self.current
                ws = cur if (cur is not None and cur.is_empty()) \
                    else self._add_workspace()
                ws._quiet = True
                try:
                    ok = ws._load_primary(path)
                finally:
                    ws._quiet = False
                if ok:
                    made[i] = ws
                elif ws.is_empty() and self.tabs.count() > 1:
                    self._remove_workspace(ws)
            active = None
            for i, ws in made.items():
                ent = entries[i]
                link = ent.get("link")
                ws._quiet = True
                try:
                    if link is not None and link in made and made[link] is not ws:
                        ws.link_comparison(made[link])
                    elif ent.get("cmp_version"):
                        self.show_workspace(ws)
                        ws.compare_with_version(ent["cmp_version"], quiet=True)
                    elif ent.get("comparison") and _os.path.exists(ent["comparison"]):
                        self.show_workspace(ws)
                        ws._load_comparison(ent["comparison"])
                finally:
                    ws._quiet = False
                self._restore_tables_of(ws, ent)
                if ent.get("active"):
                    active = ws
            if active is not None:
                self.show_workspace(active)
            return list(made.values())


def run(primary_path: Optional[str] = None,
        comparison_path: Optional[str] = None, new_instance: bool = False):
    """Launch the GUI. A file is optional -- the window opens empty and a
    project can be opened from File ▸ Open (or Open Recent). One window per
    user by default: started again (a double-clicked project), it hands the
    file to the window already running and exits; ``new_instance`` starts a
    window of its own anyway. Returns the exit code."""
    if not HAVE_QT:
        raise RuntimeError("PySide6 is not installed")
    import sys
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    from . import gui_instance
    if not new_instance and gui_instance.hand_over(primary_path):
        return 0                    # the running window has it
    from . import diagnostics
    diagnostics.install(app)    # a hang or a crash now leaves a log behind
    app.setApplicationName(branding.APP_NAME)
    app.setOrganizationName(branding.ORG)
    app.setWindowIcon(app_icon())
    from . import gui_style
    gui_style.install(app)      # Windows' "Always show scrollbars", honoured
    from . import appsettings
    appsettings.migrate()       # carry saved preferences across a rename, once
    from . import gui_theme
    gui_theme.apply_saved(app)  # Options > Appearance > Theme
    from .gui_modules import confirm_licences
    confirm_licences()          # a module installed without its licence asked
    from .gui_help import install_f1
    install_f1(app)             # F1 anywhere: help for the window in front
    # Everything start-up asks comes first, on the screen the window will
    # use, so the splash then runs once, start to finish, on that screen too.
    from . import gui_options, gui_splash
    screen = gui_options.start_screen()

    def place(dlg):
        gui_options.center_on(dlg, screen)
    recovered = _gp.ask_recovery(None, place)   # work a crash left behind
    restore = None if primary_path else MainWindow.ask_restore(None, place)
    gui_splash.begin(screen=screen)             # icon, version, progress
    gui_splash.step("Loading modules...", 0.15)
    comparison = TunerSession(comparison_path) \
        if comparison_path and not _gp.is_project(comparison_path) else None
    win = MainWindow(None, comparison)
    gui_splash.step("Opening the window...", 0.45)
    gui_options.show_main(win, screen)  # as big as last time, or maximized
    from . import gui_files as _gf
    if not new_instance:
        gui_instance.listen(win, lambda path: _gf.open_any(win, path))

    def _startup():
        try:
            if recovered:
                gui_splash.step("Recovering unsaved work...", 0.5)
                _gp.recover_chosen(win, recovered)
            if primary_path:
                gui_splash.step("Opening %s..." % _os.path.basename(primary_path),
                                0.7)
                _gf.open_any(win, primary_path)
                if comparison_path and comparison is None:
                    _gf.compare_with_file(win, comparison_path)   # a project
            elif restore:
                win.restore_tabs(*restore)  # last session's, as chosen above
        finally:
            gui_splash.end()
    QtCore.QTimer.singleShot(0, _startup)
    from .gui_update import schedule_startup_check
    schedule_startup_check(win)     # once a day, a few seconds after start-up
    return app.exec()


def _set_windows_app_id(app_id: str):
    """Give this process its own Windows AppUserModelID. Without one, Windows
    groups every window of OVTSuite.exe under one taskbar button showing the
    exe's icon, so a standalone tool would look like the main app. Must run before
    the first window is created; a no-op off Windows or if the call fails."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(app_id))
    except Exception:
        pass


def _run_tool(tool: str, app_id: str, make_dialog):
    """Run one tool's dialog as the whole application, with no main window.

    Shared by the ``--flasher``, ``--sniffer`` and ``--logger`` launch modes:
    its own taskbar identity (``app_id``) and icon (``tool``), the same
    diagnostics and settings migration as the main app, then ``make_dialog()``
    run modally. Returns the exit code."""
    if not HAVE_QT:
        raise RuntimeError("PySide6 is not installed")
    import sys
    _set_windows_app_id(app_id)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    from . import diagnostics
    diagnostics.install(app)
    app.setApplicationName(branding.APP_NAME)
    app.setOrganizationName(branding.ORG)
    use_tool_icon(tool)
    app.setWindowIcon(app_icon())
    from . import gui_style
    gui_style.install(app)
    from . import appsettings
    appsettings.migrate()
    from . import gui_theme
    gui_theme.apply_saved(app)
    from .gui_modules import confirm_licences
    confirm_licences()
    from . import gui_splash
    gui_splash.begin(tool)
    gui_splash.step("Loading...", 0.4)
    try:
        dlg = make_dialog()
    finally:
        gui_splash.end()
    from .gui_help import install_f1
    install_f1(app)
    if tool in branding.TOOL_NAMES:
        # no main window, so no Help menu: help, the version, credits and
        # licences are a button away instead
        from .gui_about import add_tool_buttons
        add_tool_buttons(dlg, tool)
    dlg.exec()
    # Escape reaches reject(), which only hides the dialog -- its closeEvent,
    # where the sniffer and logger stop their workers, never runs. In the main
    # app the main window closes them later; here nothing would, and the process
    # would exit under a live worker thread. close() delivers the event either
    # way, and both closeEvents are safe to run twice.
    dlg.close()
    return 0


def run_flasher(path: Optional[str] = None):
    """Launch only the ECU flasher (``ovtsuite --flasher [file]``), with no main
    window. The file to write is chosen in the dialog, or passed here. Returns
    the exit code."""
    def make():
        from .flash.gui_flash import FlashDialog
        from .flash.standalone import StandaloneFlashHost
        dlg = FlashDialog(StandaloneFlashHost(), "write" if path else "read")
        if path:
            dlg.load_file(path)
        return dlg
    return _run_tool("flasher", branding.FLASHER_APP_ID, make)


def run_install_module(path: str):
    """Install a ``.ovtmod`` the user opened (double-click / file
    association): confirm, install for this user, report. No main window."""
    def make():
        from .gui_modules import ModuleManagerDialog, install_from_file
        dlg = ModuleManagerDialog()
        QtCore.QTimer.singleShot(0, lambda: (install_from_file(dlg, path),
                                             dlg.refresh()))
        return dlg
    return _run_tool("app", branding.EXE_NAME, make)


def run_sniffer():
    """Launch only the bus sniffer (``ovtsuite --sniffer``), with no main
    window. It never needed one: it reads the bus, not a binary."""
    def make():
        from .sniff.gui_sniff import SnifferDialog
        return SnifferDialog(None)
    return _run_tool("sniffer", branding.SNIFFER_APP_ID, make)


def run_logger(paths=()):
    """Launch only the data logger (``ovtsuite --logger [file ...]``), with no
    main window. Binaries name the measuring-block cells; they are opened in
    the dialog (Open binary...), or passed here -- one per controller, so an
    engine file and a transmission file can both be given for dual logging."""
    def make():
        from .datalog.gui_log import DataLogDialog
        from .datalog.standalone import StandaloneLogHost
        dlg = DataLogDialog(StandaloneLogHost())
        for p in paths:
            dlg.open_binary(p)
        return dlg
    return _run_tool("logger", branding.LOGGER_APP_ID, make)


def run_simulator():
    """Launch only the bench ECU simulator (``ovtsuite --simulator``), with no
    main window. It answers on the CAN bus as a chosen module to capture a
    tool's command/read sequence; it needs no binary of its own."""
    def make():
        from .flash.gui_sim import SimulatorDialog
        return SimulatorDialog(None)
    return _run_tool("simulator", branding.SIMULATOR_APP_ID, make)
