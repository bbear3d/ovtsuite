# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: the window (Tools > Converter... and the standalone tool).

One screen, top to bottom: the files to convert (dropped or browsed) and an
optional binary; the target format with only that format's options; the
transforms (filter, translate); a preview of what will be written, with the
tables a target cannot express marked; then Convert.

What the files are decides what can be made: definitions convert to other
definitions or, with a binary, to calibration values (DCM, CDFX); value
files convert to each other; a definition with value files writes the values
into a copy of the binary; two definitions can be compared. A definition
made for another software version than the binary is ported onto it: every
table found by its own evidence. Every setting saves as a
preset the command line takes too (``ovtsuite convert --preset``).

The work is :func:`ovtsuite.convert.run`; this only builds the job.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from . import convert as C
from .convert import translate as T

#: a cell that can be selected but not typed in
_READ_ONLY = QtCore.Qt.ItemFlag.ItemIsSelectable | QtCore.Qt.ItemFlag.ItemIsEnabled

#: settings of the window, beside the translator's
_STATE = "window.json"
_PRESETS = "presets"


def _cfg_path(name: str) -> str:
    return os.path.join(T.config_dir(), name)


def _int_or_none(text: str) -> Optional[int]:
    text = (text or "").strip().replace("_", "")
    if not text:
        return None
    return int(text, 0)


# ------------------------------------------------------------------ worker
class _Worker(QtCore.QThread):
    """Runs a job (or a preview) off the GUI thread."""
    progress = QtCore.Signal(str)
    done = QtCore.Signal(object, object)          # result, error

    def __init__(self, job: C.Job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        try:
            res = C.run(self.job, progress=self.progress.emit)
            self.done.emit(res, None)
        except Exception as exc:                  # shown, not raised
            self.done.emit(None, exc)


class _PreviewWorker(QtCore.QThread):
    done = QtCore.Signal(object, object)

    def __init__(self, job: C.Job, parent=None):
        super().__init__(parent)
        self.job = job
        self.tmp = ""                         # removed once the preview is shown

    def run(self):
        try:
            d = self.tmp = tempfile.mkdtemp(prefix="ovtconv")
            job = C.Job.from_dict(self.job.to_dict())
            job.out = os.path.join(d, "preview" + C.EXT[job.target])
            kinds = C.classify(job.sources)
            if job.target == C.DIFF:
                res = C.run(job)
                self.done.emit((res, None), None)
                return
            if job.target in C.VAL or (job.target in C.IMG and kinds["values"]):
                res = C.run(job)
                self.done.emit((res, _values_shown(job, res, kinds)), None)
                return
            if job.target in C.IMG:
                res = C.run(job)
                self.done.emit((res, None), None)
                return
            res = C.run(job)
            if job.options.get("port"):
                # the tables as ported (the run placed them once already)
                with open(res.out[0], "rb") as fh:
                    defn = C.load_definition(fh.read()) if job.target != "dam" else None
                if defn is None:
                    defn = C.prepare(job)[0]
            else:
                defn = C.prepare(job)[0]
            self.done.emit((res, defn), None)
        except Exception as exc:
            self.done.emit(None, exc)


def _values_shown(job: C.Job, res: C.Result, kinds) -> Optional[object]:
    """The values to list for a value job: those written (a value file), or
    those asked for (applied to a binary)."""
    from .convert import calvalues as V
    try:
        if job.target in C.VAL:
            with open(res.out[0], "rb") as fh:
                return V.load(fh.read())
        sets = []
        for p in kinds["values"]:
            with open(p, "rb") as fh:
                sets.append(V.load(fh.read()))
        return V.merge(sets)
    except Exception:
        return None


# ------------------------------------------------------------------ pieces
class _DropList(QtWidgets.QListWidget):
    """The source files: drop files on it, or Add files..."""
    added = QtCore.Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(70)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.added.emit(paths)
            ev.acceptProposedAction()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.count() == 0:
            p = QtGui.QPainter(self.viewport())
            p.setPen(self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText))
            p.drawText(self.viewport().rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "Drop definitions, value files or binaries here\n"
                       "(XDF, A2L, DAM, OLS, KP, JSON, CSV, DCM, CDFX, BIN, HEX, S-record)")


class TranslatorSettings(QtWidgets.QDialog):
    """The default online translator and its key (keys go to the system's
    key store, as the AI service keys do)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Online translator")
        cfg = T.settings()
        f = QtWidgets.QFormLayout(self)
        self.provider = QtWidgets.QComboBox()
        for k, v in T.PROVIDERS.items():
            self.provider.addItem(v, k)
        i = self.provider.findData(cfg.get("provider"))
        self.provider.setCurrentIndex(max(0, i))
        f.addRow("Default translator:", self.provider)
        self.key = QtWidgets.QLineEdit()
        self.key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("unchanged")
        f.addRow("Key:", self.key)
        self.url = QtWidgets.QLineEdit(cfg.get("libretranslate_url", T.DEFAULT_LIBRE))
        f.addRow("LibreTranslate address:", self.url)
        self.region = QtWidgets.QLineEdit(cfg.get("microsoft_region", ""))
        self.region.setPlaceholderText("e.g. westeurope (Microsoft only)")
        f.addRow("Microsoft region:", self.region)
        note = QtWidgets.QLabel(
            "Only the texts are sent, never binary data. The AI choice uses the "
            "service set up in Options > AI.")
        note.setWordWrap(True)
        f.addRow(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.provider.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        p = self.provider.currentData()
        self.key.setEnabled(p != "ai")
        self.url.setEnabled(p == "libretranslate")
        self.region.setEnabled(p == "microsoft")

    def _save(self):
        p = self.provider.currentData()
        T.save_settings(provider=p, libretranslate_url=self.url.text().strip() or T.DEFAULT_LIBRE,
                        microsoft_region=self.region.text().strip())
        if self.key.text().strip() and p != "ai":
            try:
                T.set_key(p, self.key.text().strip())
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Online translator",
                                              "The key could not be stored: %s" % exc)
                return
        self.accept()


class ReviewDialog(QtWidgets.QDialog):
    """Every text and its translation, editable before anything is written."""

    def __init__(self, rows: List[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review translation")
        self.resize(900, 560)
        self.rows = rows
        v = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.only = QtWidgets.QCheckBox("Only texts that change")
        self.only.setChecked(True)
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText("Find...")
        top.addWidget(self.only)
        top.addWidget(self.find, 1)
        v.addLayout(top)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Table", "Field", "Original", "Translation", "From"])
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        v.addWidget(self.table, 1)
        n, chars = T.online_cost(rows)
        self.info = QtWidgets.QLabel(
            "%d texts would go to the online translator (%d characters)." % (n, chars)
            if n else "Every text is translated offline (glossary, memory, built-in).")
        v.addWidget(self.info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.only.toggled.connect(self._fill)
        self.find.textChanged.connect(self._fill)
        self._fill()

    def _visible(self):
        f = self.find.text().strip().lower()
        for i, r in enumerate(self.rows):
            if self.only.isChecked() and r["result"] == r["source"]:
                continue
            if f and f not in (r["source"] + " " + r["result"] + " " + r["table"]).lower():
                continue
            yield i, r

    def _fill(self):
        self._commit()
        self.table.setRowCount(0)
        self._index = []
        for i, r in self._visible():
            k = self.table.rowCount()
            self.table.insertRow(k)
            self._index.append(i)
            for c, val in enumerate((r["table"], r["field"], r["source"], r["result"], r["by"])):
                it = QtWidgets.QTableWidgetItem(val)
                if c != 3:
                    it.setFlags(_READ_ONLY)
                self.table.setItem(k, c, it)

    def _commit(self):
        for k, i in enumerate(getattr(self, "_index", [])):
            it = self.table.item(k, 3)
            if it is not None and it.text() != self.rows[i]["result"]:
                self.rows[i]["result"] = it.text()
                self.rows[i]["by"] = "you"

    def _accept(self):
        self._commit()
        self.accept()


# ------------------------------------------------------------------ the window
class ConverterDialog(QtWidgets.QDialog):
    """Definitions and binaries, from one format to another."""

    def __init__(self, parent=None, sources=(), binary: str = ""):
        super().__init__(parent)
        self.setWindowTitle(branding.TOOL_NAMES.get("converter", "OVT Converter"))
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self._worker = None
        self._reviewed: Optional[List[dict]] = None
        self._last: Optional[C.Result] = None
        self._kinds = {}                       # path -> ("definition"|"values"|"image", type)

        # sections 1-3 scroll above a preview the splitter sizes, so the
        # window fits a short screen (and keeps the size it was left at)
        root = QtWidgets.QVBoxLayout(self)
        self._top = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(self._top)
        outer.setContentsMargins(0, 0, 0, 0)

        # 1. sources
        g = QtWidgets.QGroupBox("1. Files to convert")
        g.setToolTip("Definitions convert to other formats, or with the binary to "
                     "calibration values (DCM, CDFX).\nValue files convert to each "
                     "other.\nA definition together with value files writes the "
                     "values into a copy of the binary.")
        gl = QtWidgets.QVBoxLayout(g)
        self.sources = _DropList()
        self.sources.setMinimumHeight(60)
        self.sources.added.connect(self.add_sources)
        gl.addWidget(self.sources)
        row = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("Add files...")
        b_add.clicked.connect(self._browse_sources)
        b_rm = QtWidgets.QPushButton("Remove")
        b_rm.clicked.connect(self._remove_selected)
        row.addWidget(b_add)
        row.addWidget(b_rm)
        row.addStretch(1)
        gl.addLayout(row)
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(QtWidgets.QLabel("Binary (optional):"))
        self.binary = QtWidgets.QLineEdit(binary)
        self._binary_hint = ("settles the base address, sizes stored in the "
                             "image and the checksum plugin")
        self.binary.setPlaceholderText(self._binary_hint)
        brow.addWidget(self.binary, 1)
        b_bin = QtWidgets.QPushButton("Browse...")
        b_bin.clicked.connect(self._browse_binary)
        brow.addWidget(b_bin)
        gl.addLayout(brow)
        # porting onto another software version
        self.port = QtWidgets.QCheckBox(
            "The binary is another software version: find each table in it (port)")
        self.port.setToolTip(
            "The definition was made for other software than the binary. Every table "
            "is placed by its own evidence (its stored header and axes, the tables "
            "around it, and the original image's bytes when given) instead of one "
            "offset. Tables nothing confirms are set apart.")
        gl.addWidget(self.port)
        self.port_box = QtWidgets.QWidget()
        pb = QtWidgets.QHBoxLayout(self.port_box)
        pb.setContentsMargins(20, 0, 0, 0)
        pb.addWidget(QtWidgets.QLabel("Original image (optional):"))
        self.reference = QtWidgets.QLineEdit()
        self.reference.setPlaceholderText("the binary / HEX the definition was made for: "
                                          "finds moved tables by their bytes")
        pb.addWidget(self.reference, 1)
        b_ref = QtWidgets.QPushButton("Browse...")
        b_ref.clicked.connect(self._browse_reference)
        pb.addWidget(b_ref)
        self.unverified = QtWidgets.QComboBox()
        self.unverified.addItem("Unconfirmed tables: in 'Unverified'", "category")
        self.unverified.addItem("Unconfirmed tables: leave out", "skip")
        pb.addWidget(self.unverified)
        gl.addWidget(self.port_box)
        self.port_box.setVisible(False)
        self.port.toggled.connect(self._port_toggled)
        # a RomRaider file holds many ROMs: which one
        self.rom_box = QtWidgets.QWidget()
        rb = QtWidgets.QHBoxLayout(self.rom_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.addWidget(QtWidgets.QLabel("ROM:"))
        self.rom = QtWidgets.QComboBox()
        self.rom.setToolTip(
            "A RomRaider definition file describes many ROMs. Use the one whose id "
            "string is in the binary, or pick one.")
        self.rom.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.rom.setMinimumContentsLength(30)
        rb.addWidget(self.rom, 1)
        self.rom_found = QtWidgets.QLabel()
        rb.addWidget(self.rom_found)
        gl.addWidget(self.rom_box)
        self.rom_box.setVisible(False)
        self._rom_src = None                 # the RomRaider file the list is from
        self._rom_ids = []                   # its (xmlid, id string, id address, text)
        self._rom_default = None             # its ROM when none is named / found
        self._want_rom = None                # from a preset, until the file is added
        self._bin_seen = (None, None)        # (binary key, the ROM found in it)
        self.binary.textChanged.connect(self._update_rom_found)
        self.rom.currentIndexChanged.connect(self._update_rom_found)
        outer.addWidget(g)

        # 2. target
        g = QtWidgets.QGroupBox("2. Convert to")
        tl = QtWidgets.QFormLayout(g)
        self.target = QtWidgets.QComboBox()
        self.target.currentIndexChanged.connect(self._target_changed)
        tl.addRow("Format:", self.target)
        self.opts = QtWidgets.QStackedWidget()
        self._opt_pages = {}
        self._build_option_pages()
        tl.addRow(self.opts)
        place = QtWidgets.QHBoxLayout()
        self.size = QtWidgets.QLineEdit()
        self.size.setPlaceholderText("from the binary / definition")
        self.base = QtWidgets.QLineEdit()
        self.base.setPlaceholderText("from the binary / definition")
        place.addWidget(QtWidgets.QLabel("Image size:"))
        place.addWidget(self.size)
        place.addWidget(QtWidgets.QLabel("Offset 0 is address:"))
        place.addWidget(self.base)
        tl.addRow(place)
        self.dynamic = QtWidgets.QComboBox()
        self.dynamic.addItem("Write at the declared maximum", "max")
        self.dynamic.addItem("Leave them out", "skip")
        tl.addRow("Tables sized by the image, with no binary:", self.dynamic)
        outer.addWidget(g)

        # 3. transforms
        g = QtWidgets.QGroupBox("3. Change on the way (optional)")
        xl = QtWidgets.QVBoxLayout(g)
        self.filter_box = QtWidgets.QGroupBox("Only some tables")
        self.filter_box.setCheckable(True)
        self.filter_box.setChecked(False)
        fl = QtWidgets.QFormLayout(self.filter_box)
        self.pattern = QtWidgets.QLineEdit()
        self.pattern.setPlaceholderText("regular expression on name, symbol or description")
        fl.addRow("Name matches:", self.pattern)
        self.categories = QtWidgets.QLineEdit()
        self.categories.setPlaceholderText("category names, separated by ;")
        fl.addRow("In categories:", self.categories)
        xl.addWidget(self.filter_box)
        self.tr_box = QtWidgets.QGroupBox("Translate")
        self.tr_box.setCheckable(True)
        self.tr_box.setChecked(False)
        trl = QtWidgets.QGridLayout(self.tr_box)
        self.tr_backend = QtWidgets.QComboBox()
        self.tr_backend.addItem("Built-in (German to English, offline)", "builtin")
        self.tr_backend.addItem("Online", "online")
        self.tr_provider = QtWidgets.QComboBox()
        self.tr_provider.addItem("Default (%s)" % T.PROVIDERS.get(T.settings().get("provider"), ""), "")
        for k, v in T.PROVIDERS.items():
            self.tr_provider.addItem(v, k)
        self.tr_from = QtWidgets.QComboBox()
        self.tr_from.setEditable(True)
        self.tr_from.addItems(["auto", "de", "en", "fr", "it", "es", "ja"])
        self.tr_to = QtWidgets.QComboBox()
        self.tr_to.setEditable(True)
        self.tr_to.addItems(["en", "de", "fr", "it", "es", "pl", "ru"])
        self.tr_mode = QtWidgets.QComboBox()
        self.tr_mode.addItem("Replace the text", "replace")
        self.tr_mode.addItem("Both: original / translation", "bilingual")
        self.tr_mode.addItem("Translation, original in the description", "keep")
        b_review = QtWidgets.QPushButton("Review...")
        b_review.clicked.connect(self._review)
        b_set = QtWidgets.QPushButton("Translator settings...")
        b_set.clicked.connect(lambda: TranslatorSettings(self).exec())
        trl.addWidget(QtWidgets.QLabel("Using:"), 0, 0)
        trl.addWidget(self.tr_backend, 0, 1)
        trl.addWidget(self.tr_provider, 0, 2)
        trl.addWidget(QtWidgets.QLabel("From / to:"), 1, 0)
        fromto = QtWidgets.QHBoxLayout()
        fromto.addWidget(self.tr_from)
        fromto.addWidget(QtWidgets.QLabel("to"))
        fromto.addWidget(self.tr_to)
        fromto.addStretch(1)
        trl.addLayout(fromto, 1, 1)
        trl.addWidget(self.tr_mode, 1, 2)
        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(b_review)
        btns.addWidget(b_set)
        btns.addStretch(1)
        trl.addLayout(btns, 2, 0, 1, 3)
        self.tr_backend.currentIndexChanged.connect(
            lambda: self.tr_provider.setEnabled(self.tr_backend.currentData() == "online"))
        self.tr_provider.setEnabled(False)
        for w in (self.tr_backend, self.tr_provider, self.tr_from, self.tr_to):
            sig = getattr(w, "currentIndexChanged")
            sig.connect(self._forget_review)
        xl.addWidget(self.tr_box)
        outer.addWidget(g)
        for box in (self.filter_box, self.tr_box):
            _fold_when_off(box)
        outer.addStretch(1)
        self._top_scroll = QtWidgets.QScrollArea()
        self._top_scroll.setWidgetResizable(True)
        self._top_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._top_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._top_scroll.setWidget(self._top)
        self.split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self._top_scroll)
        root.addWidget(self.split, 1)
        outer = root                          # the rest goes under the splitter

        # 4. preview
        g = QtWidgets.QGroupBox("4. Preview")
        pl = QtWidgets.QVBoxLayout(g)
        prow = QtWidgets.QHBoxLayout()
        self.b_preview = QtWidgets.QPushButton("Preview")
        self.b_preview.clicked.connect(self.preview)
        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        prow.addWidget(self.b_preview)
        prow.addWidget(self.summary, 1)
        pl.addLayout(prow)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "Table", "Address", "Size", "Type", "Units"])
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(80)
        pl.addWidget(self.table, 1)
        self.split.addWidget(g)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)

        # 5. output
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(QtWidgets.QLabel("Save as:"))
        self.out = QtWidgets.QLineEdit()
        orow.addWidget(self.out, 1)
        b_out = QtWidgets.QPushButton("Browse...")
        b_out.clicked.connect(self._browse_out)
        orow.addWidget(b_out)
        outer.addLayout(orow)
        brow = QtWidgets.QHBoxLayout()
        self.presets = QtWidgets.QComboBox()
        self.presets.setMinimumWidth(180)
        self.presets.activated.connect(self._load_preset)
        b_save_p = QtWidgets.QPushButton("Save preset...")
        b_save_p.clicked.connect(self._save_preset)
        brow.addWidget(QtWidgets.QLabel("Preset:"))
        brow.addWidget(self.presets)
        brow.addWidget(b_save_p)
        # more: a watch folder, the right-click menu
        self.b_more = QtWidgets.QToolButton()
        self.b_more.setText("More")
        self.b_more.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QtWidgets.QMenu(self.b_more)
        self.act_watch = more.addAction("Watch a folder...")
        self.act_watch.setToolTip("Convert every file that arrives in a folder with "
                                  "these settings")
        self.act_watch.triggered.connect(self.open_watch)
        from .convert import shellmenu as _sm
        self.act_shell = more.addAction(
            "Add \"%s\" to the right-click menu" % _sm.LABEL)
        self.act_shell.setCheckable(True)
        self.act_shell.setVisible(_sm.supported())
        self.act_shell.triggered.connect(self._toggle_shell_menu)
        more.aboutToShow.connect(self._sync_more)
        more.setToolTipsVisible(True)
        self.b_more.setMenu(more)
        brow.addWidget(self.b_more)
        self._watch_dlg = None
        self.binary.textChanged.connect(self._watch_refresh)
        brow.addStretch(1)
        self.status = QtWidgets.QLabel("")
        brow.addWidget(self.status)
        self.b_report = QtWidgets.QPushButton("Report...")
        self.b_report.setEnabled(False)
        self.b_report.clicked.connect(self._show_report)
        self.b_folder = QtWidgets.QPushButton("Open folder")
        self.b_folder.setEnabled(False)
        self.b_folder.clicked.connect(self._open_folder)
        self.b_convert = QtWidgets.QPushButton("Convert")
        self.b_convert.setDefault(True)
        self.b_convert.clicked.connect(self.convert)
        brow.addWidget(self.b_report)
        brow.addWidget(self.b_folder)
        brow.addWidget(self.b_convert)
        outer.addLayout(brow)

        self._refresh_presets()
        self._restore_state()
        if sources:
            self.add_sources(list(sources))
        self._sync_targets()
        self._fit_window()

    # ---------------------------------------------------------- options
    def _build_option_pages(self):
        def page():
            w = QtWidgets.QWidget()
            f = QtWidgets.QFormLayout(w)
            f.setContentsMargins(0, 0, 0, 0)
            return w, f

        w, f = page()
        self.cks = QtWidgets.QComboBox()
        f.addRow("Checksum plugin:", self.cks)
        self.cks.currentIndexChanged.connect(self._cks_changed)
        self.cks_note = QtWidgets.QLabel("")
        self.cks_note.setWordWrap(True)
        f.addRow(self.cks_note)
        self.indexed = QtWidgets.QCheckBox("Include value blocks without an axis")
        self.indexed.setChecked(True)
        f.addRow(self.indexed)
        prow = QtWidgets.QHBoxLayout()
        self.patch_with = QtWidgets.QLineEdit()
        self.patch_with.setPlaceholderText("optional: a changed binary -- its changes become "
                                           "a TunerPro patch (the binary above is the original)")
        prow.addWidget(self.patch_with, 1)
        b_pw = QtWidgets.QPushButton("Browse...")
        b_pw.clicked.connect(self._browse_patch)
        prow.addWidget(b_pw)
        f.addRow("Add a patch: the changes in", prow)
        self.patch_opts = QtWidgets.QWidget()
        po = QtWidgets.QHBoxLayout(self.patch_opts)
        po.setContentsMargins(0, 0, 0, 0)
        self.patch_title = QtWidgets.QLineEdit()
        self.patch_title.setPlaceholderText("patch name (default: from the file)")
        po.addWidget(self.patch_title, 1)
        self.patch_split = QtWidgets.QComboBox()
        self.patch_split.addItem("One patch", "one")
        self.patch_split.addItem("One per changed table", "table")
        po.addWidget(self.patch_split)
        self.patch_outside = QtWidgets.QComboBox()
        self.patch_outside.addItem("Bytes outside the tables: include", "keep")
        self.patch_outside.addItem("Bytes outside the tables: leave out", "skip")
        self.patch_outside.setToolTip("Bytes no table covers: checksums (which TunerPro's "
                                      "checksum plugin recomputes on saving) and code.")
        po.addWidget(self.patch_outside)
        f.addRow(self.patch_opts)
        self.patch_opts.setEnabled(False)
        self.patch_with.textChanged.connect(
            lambda t: self.patch_opts.setEnabled(bool(t.strip())))
        self.patch_with.textChanged.connect(lambda _t: self._target_changed())
        self._opt_pages["xdf"] = self.opts.addWidget(w)
        self._fill_checksums()

        w, f = page()
        self.a2l_addr = QtWidgets.QComboBox()
        self.a2l_addr.addItem("File offsets (WinOLS, TunerPro, %s)" % branding.APP_NAME, "offset")
        self.a2l_addr.addItem("ECU addresses (INCA, CANape)", "ecu")
        f.addRow("Addresses:", self.a2l_addr)
        self._opt_pages["a2l"] = self.opts.addWidget(w)

        w, f = page()
        lab = QtWidgets.QLabel("Experimental: a map pack written here reads back in "
                               "%s; whether WinOLS opens it is not yet tested. "
                               "An A2L imports into WinOLS reliably." % branding.APP_NAME)
        lab.setWordWrap(True)
        f.addRow(lab)
        self._opt_pages["kp"] = self.opts.addWidget(w)

        w, f = page()
        lab = QtWidgets.QLabel("Writing back into a DAMOS keeps its records as they "
                               "are; from any other format a new DAMOS is written.")
        lab.setWordWrap(True)
        f.addRow(lab)
        self._opt_pages["dam"] = self.opts.addWidget(w)

        w, f = page()
        self._opt_pages["plain"] = self.opts.addWidget(w)

        w, f = page()
        self.values_note = QtWidgets.QLabel("")
        self.values_note.setWordWrap(True)
        f.addRow(self.values_note)
        self._opt_pages["values"] = self.opts.addWidget(w)

        w, f = page()
        lab = QtWidgets.QLabel("The values are written into a copy of the binary, each "
                               "where the definition places its table. Values the "
                               "definition does not have, or whose size differs, are "
                               "left alone and listed in the report.")
        lab.setWordWrap(True)
        f.addRow(lab)
        self.apply_cks = QtWidgets.QCheckBox("Correct checksums afterwards (through the "
                                             "installed module)")
        self.apply_cks.setChecked(True)
        f.addRow(self.apply_cks)
        self.apply_record = QtWidgets.QSpinBox()
        self.apply_record.setRange(1, 255)
        self.apply_record.setValue(32)
        f.addRow("Bytes per record (HEX / S-record):", self.apply_record)
        self._opt_pages["apply"] = self.opts.addWidget(w)

        w, f = page()
        self.record = QtWidgets.QSpinBox()
        self.record.setRange(1, 255)
        self.record.setValue(32)
        f.addRow("Bytes per record (HEX / S-record):", self.record)
        self.fill = QtWidgets.QLineEdit("0xFF")
        f.addRow("Fill byte for gaps:", self.fill)
        self.swap16 = QtWidgets.QCheckBox("Swap the bytes of every 16-bit word")
        f.addRow(self.swap16)
        self.fix_cks = QtWidgets.QCheckBox("Correct checksums (through the installed module)")
        f.addRow(self.fix_cks)
        self._opt_pages["image"] = self.opts.addWidget(w)

    def _fill_checksums(self):
        from .convert import checksums as K
        self.cks.clear()
        self.cks.addItem("None", "none")
        self.cks.addItem("Keep the source's", "keep")
        self.cks.addItem("Automatic (needs the binary)", "auto")
        for d in K.descriptors():
            self.cks.addItem(d.name, d.id)
        # the TunerPro plugins installed on this PC, one entry per method
        for p in K.installed_plugins():
            for i, m in enumerate(p.methods):
                label = ("TunerPro: %s" % p.name if len(p.methods) == 1
                         else "TunerPro: %s - %s" % (p.name, m))
                self.cks.addItem(label, p.choice(i))
                self.cks.setItemData(self.cks.count() - 1,
                                     "%s\n%s\n%s" % (m, p.desc, p.path),
                                     QtCore.Qt.ToolTipRole)
        self.cks.addItem("Another TunerPro plugin (DLL)...", "dll")
        self._dll = ""

    def _cks_changed(self):
        from .convert import checksums as K
        v = self.cks.currentData()
        note = ""
        if v == "dll":
            dirs = K.plugin_dirs()
            path, _f = QtWidgets.QFileDialog.getOpenFileName(
                self, "TunerPro checksum plugin", dirs[0] if dirs else "",
                "Plugin (*.dll);;All files (*)")
            if not path:
                self.cks.setCurrentIndex(0)
                return
            try:
                plug = K.parse_plugin(path)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, "Checksum plugin", str(exc))
                self.cks.setCurrentIndex(0)
                return
            if plug is None:
                QtWidgets.QMessageBox.warning(
                    self, "Checksum plugin",
                    "%s is not a TunerPro checksum plugin (a 32-bit DLL that "
                    "reports a plugin id and its checksum methods)."
                    % os.path.basename(path))
                self.cks.setCurrentIndex(0)
                return
            mi = 0
            if len(plug.methods) > 1:
                m, ok = QtWidgets.QInputDialog.getItem(
                    self, "Checksum plugin", "%s: which checksum?" % plug.name,
                    plug.methods, 0, False)
                if not ok:
                    self.cks.setCurrentIndex(0)
                    return
                mi = plug.methods.index(m)
            # the XDF names the plugin by its id and method: add it as such
            label = "TunerPro: %s - %s" % (plug.name, plug.methods[mi])
            i = self.cks.findData(plug.choice(mi))
            if i < 0:
                i = self.cks.count() - 1
                self.cks.insertItem(i, label, plug.choice(mi))
            self.cks.setCurrentIndex(i)
            return
        elif v and str(v).startswith(K.TP):
            pid = str(v)[len(K.TP):].split(":")[0]
            plug = next((q for q in K.installed_plugins() if q.uuid == pid), None)
            note = ("TunerPro computes this checksum when it saves the binary "
                    "(plugin %s%s)." % (pid, ", " + os.path.basename(plug.path)
                                        if plug else ""))
        else:
            d = K.descriptor(v) if v not in ("none", "keep", "auto") else None
            if d is not None and d.limitations:
                note = d.limitations
        self.cks_note.setText(note)
        self.cks_note.setVisible(bool(note))

    # ---------------------------------------------------------- sources
    def _browse_sources(self):
        from .recent import get_last_dir, set_last_dir
        paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Files to convert", get_last_dir("convert"),
            "Definitions, values and binaries (*.xdf *.a2l *.dam *.ols *.kp *.json *.csv "
            "*.dcm *.cdfx *.cdf *.bin *.hex *.s19 *.s28 *.s37 *.mot *.ori *.mod);;"
            "All files (*)")
        if paths:
            set_last_dir(paths[0], "convert")
            self.add_sources(paths)

    def _browse_binary(self):
        from .recent import get_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Binary (optional)", get_last_dir("binary"),
            "Binaries (*.bin *.hex *.s19 *.mot *.ori *.mod);;All files (*)")
        if path:
            self.binary.setText(path)

    def _browse_patch(self):
        from .recent import get_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Changed binary", get_last_dir("binary"),
            "Binaries (*.bin *.hex *.s19 *.mot *.ori *.mod);;All files (*)")
        if path:
            self.patch_with.setText(path)

    def _browse_reference(self):
        from .recent import get_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Original image", get_last_dir("binary"),
            "Binaries (*.bin *.hex *.s19 *.mot *.ori *.mod);;All files (*)")
        if path:
            self.reference.setText(path)

    def _port_toggled(self, on: bool):
        self.port_box.setVisible(on)
        self._target_changed()

    # ---------------------------------------------------- RomRaider ROMs
    def _romraider_source(self) -> Optional[str]:
        for p in self.source_paths():
            if self._kinds.get(p, ("", ""))[1] == "romraider":
                return p
        return None

    def _sync_roms(self):
        """Show the ROM picker while a RomRaider file is among the sources."""
        from .convert import fmt_romxml
        src = self._romraider_source()
        self.rom_box.setVisible(src is not None)
        if src == self._rom_src:
            return
        keep = self.rom.currentData() or self._want_rom
        self._rom_src, self._rom_ids, self._rom_default = src, [], None
        self.rom.blockSignals(True)
        self.rom.clear()
        if src is not None:
            try:
                with open(src, "rb") as fh:
                    data = fh.read()
                self._rom_ids = fmt_romxml.rom_ids(data)
                self._rom_default = fmt_romxml.default_rom(data)
            except Exception as exc:                            # noqa: BLE001
                self.rom_found.setText("not readable: %s" % exc)
            self.rom.addItem("Found from the binary" if self._rom_default is None else
                             "Found from the binary (else %s)" % self._rom_default, None)
            for xid, idstr, addr, desc in self._rom_ids:
                self.rom.addItem(("%s    %s" % (xid, desc)).rstrip(), xid)
                if idstr and addr is not None:
                    self.rom.setItemData(self.rom.count() - 1,
                                         "id string %r at 0x%X" % (idstr, addr),
                                         QtCore.Qt.ItemDataRole.ToolTipRole)
            i = self.rom.findData(keep) if keep else -1
            self.rom.setCurrentIndex(max(0, i))
            if i >= 0:
                self._want_rom = None
        self.rom.blockSignals(False)
        self._bin_seen = (None, None)
        self._update_rom_found()

    def _identified_rom(self) -> Optional[str]:
        """The ROM whose id string is in the binary field's file, or None."""
        path = self.binary.text().strip()
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (path, self._rom_src, st.st_size, st.st_mtime)
        if self._bin_seen[0] == key:
            return self._bin_seen[1]
        from .convert import image as _cimage
        hit = None
        try:
            with open(path, "rb") as fh:
                img = _cimage.read(fh.read()).flat()
            hits = []
            for xid, idstr, addr, _d in self._rom_ids:
                if idstr and addr is not None:
                    b = idstr.encode("latin-1", "replace")
                    if img[addr:addr + len(b)] == b:
                        hits.append(xid)
            hit = hits[0] if len(hits) == 1 else None
        except Exception:                                       # noqa: BLE001
            hit = None
        self._bin_seen = (key, hit)
        return hit

    def _update_rom_found(self, *_):
        if self._rom_src is None:
            self.rom_found.setText("")
            return
        n = len(self._rom_ids)
        if self.rom.currentData():
            self.rom_found.setText("of %d" % n)
            return
        if not self.binary.text().strip():
            self.rom_found.setText(("%d ROMs: give the binary or pick one" % n)
                                   if self._rom_default is None else "")
            return
        hit = self._identified_rom()
        self.rom_found.setText("this binary is %s" % hit if hit else
                               "not found in this binary: pick the ROM")

    def _rom_problem(self) -> str:
        """Why the RomRaider source cannot be read as set, or ``""``."""
        if self._rom_src is None or self.rom.currentData():
            return ""
        if any(k == "image" for k, _t in self._kinds.values()):
            return ""                    # the binary is among the files: found at run
        if not self.binary.text().strip():
            if self._rom_default is None:
                return ("%s holds %d ROMs: pick the one to use, or give the binary "
                        "so it is found from it." % (os.path.basename(self._rom_src),
                                                      len(self._rom_ids)))
            return ""
        if self._identified_rom() is None:
            return ("None of the %d ROMs in %s is this binary (no ROM's id string is at "
                    "its id address): pick the ROM to use."
                    % (len(self._rom_ids), os.path.basename(self._rom_src)))
        return ""

    def _browse_out(self):
        t = self.target.currentData() or ""
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save as", self.out.text(),
            "%s (*%s);;All files (*)" % (C.NAMES.get(t, t), C.EXT.get(t, "")))
        if path:
            self.out.setText(path)

    def add_sources(self, paths):
        for p in paths:
            if not os.path.isfile(p) or p in self._kinds:
                continue
            try:
                kind, t = C.sniff_file(p)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, "Converter", "%s: %s" % (p, exc))
                continue
            self._kinds[p] = (kind, t)
            it = QtWidgets.QListWidgetItem("%s    %s" % (os.path.basename(p),
                                                       C.NAMES.get(t, t)))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p)
            self.sources.addItem(it)
        self._sync_targets()
        self._forget_review()

    def _remove_selected(self):
        for it in self.sources.selectedItems():
            self._kinds.pop(it.data(QtCore.Qt.ItemDataRole.UserRole), None)
            self.sources.takeItem(self.sources.row(it))
        self._sync_targets()

    def source_paths(self) -> List[str]:
        return [self.sources.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                for i in range(self.sources.count())]

    def _all_images(self) -> bool:
        return self._mode() == "image"

    def _mode(self) -> str:
        """What the files make: ``image`` (images only), ``values`` (value
        files only), ``apply`` (a definition and value files), else
        ``definition``."""
        kinds = {k for k, _t in self._kinds.values()}
        if not kinds:
            return "definition"
        if kinds == {"image"}:
            return "image"
        if "values" in kinds:
            return "apply" if "definition" in kinds else "values"
        return "definition"

    def _sync_targets(self):
        prev = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        mode = self._mode()
        suffix = {}
        if not self._kinds:
            # nothing listed yet: every target, so a watch folder can be set
            # up for any kind of file
            fmts = list(C.DEF_OUT) + list(C.VAL) + list(C.IMG)
        elif mode == "image":
            fmts = list(C.IMG)
        elif mode == "values":
            fmts = list(C.VAL)
        elif mode == "apply":
            fmts = list(C.IMG)
            suffix = {f: "  (values written in)" for f in C.IMG}
        else:
            fmts = list(C.DEF_OUT) + list(C.VAL)
            suffix = {f: "  (values read from the binary)" for f in C.VAL}
            if sum(1 for k, _t in self._kinds.values() if k == "definition") == 2:
                fmts.append(C.DIFF)
            if any(t == "ols" for _k, t in self._kinds.values()):
                fmts += list(C.IMG)             # its embedded image
        for f in fmts:
            label = C.NAMES[f] + ("  (experimental)" if f in C.EXPERIMENTAL else "") \
                + suffix.get(f, "")
            self.target.addItem(label, f)
        i = self.target.findData(prev)
        self.target.setCurrentIndex(max(0, i))
        self.target.blockSignals(False)
        self._sync_roms()
        self._target_changed()

    def _target_changed(self):
        t = self.target.currentData() or ""
        mode = self._mode()
        has_def = any(k == "definition" for k, _t in self._kinds.values())
        self.port.setVisible(has_def)
        self.port_box.setVisible(has_def and self.port.isChecked())
        if t in C.VAL:
            page = "values"
            self.values_note.setText(
                "Every table's values are read out of the binary through the "
                "definition, with its shared axes, by name." if mode != "values" else
                "The values are converted as they are; several files are merged, a "
                "later file's value replacing an earlier one's.")
        elif t in C.IMG and mode == "apply":
            page = "apply"
        else:
            page = t if t in self._opt_pages else ("image" if t in C.IMG else "plain")
        self.opts.setCurrentIndex(self._opt_pages[page])
        is_def = t in C.DEF_OUT
        self.filter_box.setEnabled(is_def or t in C.VAL or mode == "apply" or t == C.DIFF)
        self.tr_box.setEnabled(is_def or (t in C.VAL and mode != "values"))
        self.dynamic.setEnabled(is_def)
        need = self._binary_needed()
        self.binary.setPlaceholderText(
            "needed: the binary of the other software version" if need == "port" else
            "needed: the original binary the patch changes" if need == "patch" else
            "needed: the binary to read the values from" if need == "export" else
            "needed: the binary to write the values into (or add it to the files above)"
            if need == "apply" else self._binary_hint)
        src = self.source_paths()
        if src and t:
            stem = os.path.splitext(src[0])[0]
            cur = self.out.text()
            if not cur or getattr(self, "_auto_out", "") == cur:
                ins = list(src) + [self.binary.text().strip(),
                                   self.reference.text().strip(),
                                   self.patch_with.text().strip()]
                self.out.setText(C.free_output(stem + C.EXT[t], ins))
                self._auto_out = self.out.text()
        self._watch_refresh()

    def _watch_refresh(self, *_):
        if getattr(self, "_watch_dlg", None) is not None:
            self._watch_dlg.refresh_job()

    def _binary_needed(self) -> str:
        """``"export"`` / ``"apply"`` / ``"port"`` when the job needs a
        binary, else ``""``."""
        t = self.target.currentData() or ""
        mode = self._mode()
        has_def = any(k == "definition" for k, _t in self._kinds.values())
        if self.port.isChecked() and has_def:
            return "port"
        if t == "xdf" and self.patch_with.text().strip():
            return "patch"
        if t in C.VAL and mode == "definition" and not any(
                tt == "ols" for _k, tt in self._kinds.values()):
            return "export"
        if t in C.IMG and mode == "apply":
            return "apply"
        return ""

    # ---------------------------------------------------------- the job
    def job(self) -> C.Job:
        t = self.target.currentData() or ""
        j = C.Job(sources=self.source_paths(), target=t, out=self.out.text().strip())
        j.binary = self.binary.text().strip() or None
        j.size = _int_or_none(self.size.text())
        j.base = _int_or_none(self.base.text())
        j.dynamic = self.dynamic.currentData()
        if self.filter_box.isChecked():
            f = {}
            if self.pattern.text().strip():
                f["pattern"] = self.pattern.text().strip()
            cats = [c.strip() for c in self.categories.text().split(";") if c.strip()]
            if cats:
                f["categories"] = cats
            j.filters = f
        if self.tr_box.isChecked():
            j.translate = {"backend": self.tr_backend.currentData(),
                           "provider": self.tr_provider.currentData() or "",
                           "source": self.tr_from.currentText().strip() or "auto",
                           "target": self.tr_to.currentText().strip() or "en",
                           "mode": self.tr_mode.currentData()}
            if self._reviewed is not None:
                j.translate["rows"] = self._reviewed
        if t == "xdf":
            v = self.cks.currentData()
            j.checksum = (v if v and v != "dll" else "none")
            j.options["indexed"] = self.indexed.isChecked()
            if self.patch_with.text().strip():
                j.options["patch_with"] = self.patch_with.text().strip()
                j.options["patch_split"] = self.patch_split.currentData()
                j.options["patch_outside"] = self.patch_outside.currentData()
                if self.patch_title.text().strip():
                    j.options["patch_title"] = self.patch_title.text().strip()
        elif t == "a2l":
            j.options["a2l_addresses"] = self.a2l_addr.currentData()
        elif t in C.IMG and self._mode() == "apply":
            j.options["record"] = self.apply_record.value()
            j.options["checksum"] = self.apply_cks.isChecked()
            j.options["apply_checksum"] = self.apply_cks.isChecked()
        elif t in C.IMG:
            j.options["record"] = self.record.value()
            fill = _int_or_none(self.fill.text())      # 0x00 is a fill byte too
            j.options["fill"] = 0xFF if fill is None else fill
            j.options["swap16"] = self.swap16.isChecked()
            j.options["checksum"] = self.fix_cks.isChecked()
        if self.port.isChecked() and any(k == "definition" for k, _t in self._kinds.values()):
            j.options["port"] = True
            j.options["unverified"] = self.unverified.currentData()
            if self.reference.text().strip():
                j.options["reference"] = self.reference.text().strip()
        if self._rom_src is not None and self.rom.currentData():
            j.options["rom"] = self.rom.currentData()
        return j

    def _check_ready(self) -> bool:
        if not self.source_paths():
            QtWidgets.QMessageBox.information(self, "Converter", "Add a file to convert first.")
            return False
        try:
            self.job()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Converter",
                                          "A number is not readable: %s" % exc)
            return False
        need = self._binary_needed()
        has_image = any(k == "image" for k, _t in self._kinds.values())
        if need and not self.binary.text().strip() and not has_image:
            QtWidgets.QMessageBox.information(
                self, "Converter",
                "Give the original binary: the patch is what changed from it." if need == "patch"
                else "Give the binary of the other software version to port onto." if need == "port"
                else "Give the binary to read the values from." if need == "export" else
                "Give the binary to write the values into: as the binary, or add it "
                "to the files.")
            self.binary.setFocus()
            return False
        why = self._rom_problem()
        if why:
            QtWidgets.QMessageBox.information(self, "Converter", why)
            self.rom.setFocus()
            return False
        return True

    def _forget_review(self, *_):
        self._reviewed = None

    def _review(self):
        if not self._check_ready():
            return
        job = self.job()
        job.translate = dict(job.translate or {}, backend=self.tr_backend.currentData())
        job.translate.pop("rows", None)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            defn = C.prepare(job)[0]
            if job.filters:
                from .convert.transforms import filter_tables
                defn.tables = filter_tables(defn.tables, job.filters)
            warnings: List[str] = []
            rows = T.plan(defn, job.translate, warnings)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "Review translation", str(exc))
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        if warnings:
            QtWidgets.QMessageBox.information(self, "Review translation", "\n".join(warnings))
        dlg = ReviewDialog(rows, self)
        if dlg.exec():
            self._reviewed = dlg.rows
            self.tr_box.setChecked(True)

    # ---------------------------------------------------------- preview
    def preview(self):
        if not self._check_ready() or self._worker is not None:
            return
        self._busy(True, "Preparing the preview...")
        w = _PreviewWorker(self.job(), self)
        w.done.connect(self._preview_done)
        w.finished.connect(lambda w=w: shutil.rmtree(w.tmp, ignore_errors=True)
                           if w.tmp else None)
        self._worker = w
        w.start()

    def _preview_done(self, out, err):
        self._worker = None
        self._busy(False, "")
        if err is not None:
            self.summary.setText("Cannot convert: %s" % err)
            return
        res, defn = out
        self._show_result(res, defn, preview=True)

    def _show_values(self, res: C.Result, vs) -> None:
        warn = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning)
        kinds = {"value": "value", "block": "block", "curve": "curve", "map": "map",
                 "axis": "shared axis", "text": "text", "cuboid": "3-D table"}
        for p in vs.params:
            k = self.table.rowCount()
            self.table.insertRow(k)
            lost = res.loss.get(p.name)
            mark = QtWidgets.QTableWidgetItem()
            if lost:
                mark.setIcon(warn)
                mark.setToolTip("\n".join(lost))
                mark.setData(QtCore.Qt.ItemDataRole.UserRole, 1)
            self.table.setItem(k, 0, mark)
            it = QtWidgets.QTableWidgetItem(p.name)
            it.setToolTip(p.description or "")
            self.table.setItem(k, 1, it)
            self.table.setItem(k, 2, QtWidgets.QTableWidgetItem(""))
            size = "1" if p.kind in ("value", "text") else (
                "%d x %d" % (p.ny, p.nx) if p.ny > 1 else "%d" % p.nx)
            self.table.setItem(k, 3, QtWidgets.QTableWidgetItem(size))
            self.table.setItem(k, 4, QtWidgets.QTableWidgetItem(kinds.get(p.kind, p.kind)))
            self.table.setItem(k, 5, QtWidgets.QTableWidgetItem(p.unit or ""))

    def _show_diff(self, res: C.Result) -> None:
        self.table.setHorizontalHeaderLabels(["", "Table", "Change", "First", "Second", ""])
        for r in res.diff:
            k = self.table.rowCount()
            self.table.insertRow(k)
            self.table.setItem(k, 0, QtWidgets.QTableWidgetItem())
            for col, key in ((1, "name"), (2, "change"), (3, "old"), (4, "new")):
                self.table.setItem(k, col, QtWidgets.QTableWidgetItem(r[key]))
            self.table.setItem(k, 5, QtWidgets.QTableWidgetItem(""))

    def _show_result(self, res: C.Result, defn, preview: bool):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(["", "Table", "Address", "Size", "Type", "Units"])
        if res.diff or (self.target.currentData() == C.DIFF):
            self._show_diff(res)
            defn = None
        from .convert.calvalues import ValueSet
        if isinstance(defn, ValueSet):
            self._show_values(res, defn)
            defn = None
        if defn is not None:
            warn = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning)
            for t in defn.tables:
                k = self.table.rowCount()
                self.table.insertRow(k)
                lost = res.loss.get(t.symbol or t.name) or res.loss.get(t.name)
                mark = QtWidgets.QTableWidgetItem()
                if lost:
                    mark.setIcon(warn)
                    mark.setToolTip("\n".join(lost))
                    mark.setData(QtCore.Qt.ItemDataRole.UserRole, 1)
                self.table.setItem(k, 0, mark)
                name = t.name if not t.symbol or t.symbol == t.name else "%s  (%s)" % (t.name, t.symbol)
                it = QtWidgets.QTableWidgetItem(name)
                it.setToolTip(t.description or "")
                self.table.setItem(k, 1, it)
                self.table.setItem(k, 2, QtWidgets.QTableWidgetItem(
                    "0x%X" % t.z.addr if t.z.addr is not None else ""))
                self.table.setItem(k, 3, QtWidgets.QTableWidgetItem(
                    "1" if t.kind == "constant" else "%d x %d" % (t.rows, t.cols)))
                self.table.setItem(k, 4, QtWidgets.QTableWidgetItem(
                    "%s%s %d-bit" % ("float " if t.z.float else
                                    ("signed " if t.z.signed else ""), t.kind, t.z.bits)))
                self.table.setItem(k, 5, QtWidgets.QTableWidgetItem(t.z.units or ""))
        self.table.resizeColumnToContents(0)
        self.table.setSortingEnabled(True)
        bits = []
        if res.placement:
            words = {"exact": "where the definition puts them", "moved": "found moved",
                     "region": "moved with their region", "bracketed": "between confirmed "
                     "tables", "stated": "as stated", "unverified": "unverified"}
            bits.append("ported: " + ", ".join("%d %s" % (n, words.get(g, g))
                                               for g, n in res.placement.items() if n))
        if res.values:
            bits.append(res.values[0].rstrip("."))
        elif res.tables_in or res.tables_out:
            bits.append("%d tables %s" % (res.tables_out, "will be written" if preview else "written"))
        if res.size:
            bits.append("image 0x%X bytes, offset 0 = address 0x%X" % (res.size, res.base))
        if res.loss:
            bits.append("%d not expressed fully (marked)" % len(res.loss))
        if res.check:
            bits.append("read-back check: %d differences" % len(res.check))
        elif not preview:
            bits.append("read-back check passed")
        notes = [w for w in res.warnings if not (res.placement and w.startswith("Ported onto"))]
        if notes:
            bits.append(notes[0] + (" (+%d more notes)" % (len(notes) - 1)
                                    if len(notes) > 1 else ""))
        self.summary.setText("; ".join(bits) + ".")
        self._last = res
        self.b_report.setEnabled(True)

    # ---------------------------------------------------------- convert
    def convert(self):
        if not self._check_ready() or self._worker is not None:
            return
        job = self.job()
        if not job.out:
            self._browse_out()
            job.out = self.out.text().strip()
            if not job.out:
                return
        hit = C.replaces_input(job, job.out)
        if hit:
            QtWidgets.QMessageBox.warning(
                self, "Converter",
                "The output would replace %s, which the conversion reads. "
                "Choose another name." % os.path.basename(hit))
            return
        self._busy(True, "Converting...")
        w = _Worker(job, self)
        w.progress.connect(lambda m: self.status.setText(m + "..."))
        w.done.connect(self._convert_done)
        self._worker = w
        w.start()

    def _convert_done(self, res, err):
        self._worker = None
        self._busy(False, "")
        if err is not None:
            QtWidgets.QMessageBox.warning(self, "Converter", "Not converted: %s" % err)
            return
        defn = None
        try:
            t = self.target.currentData()
            if t in C.DEF_OUT:
                defn = C.load_definition(open(res.out[0], "rb").read())
            elif t in C.VAL or self._mode() == "apply":
                defn = _values_shown(self.job(), res, C.classify(self.source_paths()))
        except Exception:
            defn = None
        self._show_result(res, defn, preview=False)
        self.status.setText("Saved %s" % os.path.basename(res.out[0]))
        self.b_folder.setEnabled(True)
        self._save_state()

    def _busy(self, on: bool, text: str):
        for b in (self.b_convert, self.b_preview):
            b.setEnabled(not on)
        self.status.setText(text)
        if on:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.BusyCursor)
        else:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _show_report(self):
        if self._last is None:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Converter report")
        dlg.resize(760, 520)
        v = QtWidgets.QVBoxLayout(dlg)
        te = QtWidgets.QPlainTextEdit(self._last.report or C.report(self.job(), self._last))
        te.setReadOnly(True)
        v.addWidget(te)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()

    def _open_folder(self):
        if self._last and self._last.out:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(
                os.path.dirname(os.path.abspath(self._last.out[0]))))

    # ------------------------------------------- watch folder, shell menu
    def open_watch(self):
        if self._watch_dlg is None:
            self._watch_dlg = WatchDialog(self)
        self._watch_dlg.refresh_job()
        self._watch_dlg.show()
        self._watch_dlg.raise_()
        self._watch_dlg.activateWindow()
        return self._watch_dlg

    def _sync_more(self):
        from .convert import shellmenu as _sm
        self.act_shell.blockSignals(True)
        self.act_shell.setChecked(bool(_sm.installed()))
        self.act_shell.blockSignals(False)

    def _toggle_shell_menu(self, on: bool):
        from .convert import shellmenu as _sm
        try:
            if on:
                where = _sm.install()
                QtWidgets.QMessageBox.information(
                    self, "Right-click menu",
                    "\"%s\" is now in the right-click menu of: %s.\n\nIt opens the "
                    "files in this window; files right-clicked together all open in "
                    "one window. Nothing else changes: double-clicking a file still "
                    "opens what opened it before.%s" % (
                        _sm.LABEL, ", ".join(where),
                        "\n\nOn Windows 11 it is under \"Show more options\"."
                        if os.name == "nt" else ""))
            else:
                _sm.remove(everywhere=True)
                self.status.setText("Removed from the right-click menu.")
        except _sm.ShellMenuError as exc:
            QtWidgets.QMessageBox.warning(self, "Right-click menu", str(exc))
        self._sync_more()

    def watch_job(self) -> C.Job:
        """The window's settings as a watch-folder job: the listed files stay
        fixed, every arriving file joins them."""
        j = self.job()
        j.out = ""
        return j

    def closeEvent_watch(self):
        if self._watch_dlg is not None:
            self._watch_dlg.stop()

    # ---------------------------------------------------------- presets
    def _preset_dir(self) -> str:
        return _cfg_path(_PRESETS)

    def _refresh_presets(self):
        self.presets.clear()
        self.presets.addItem("(none)", "")
        d = self._preset_dir()
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                if n.endswith(".json"):
                    self.presets.addItem(n[:-5], os.path.join(d, n))

    def _save_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save preset", "Name:")
        name = "".join(ch for ch in (name or "") if ch.isalnum() or ch in " -_").strip()
        if not ok or not name:
            return
        d = self.job().to_dict()
        for k in ("sources", "out", "binary"):
            d.pop(k, None)
        if d.get("translate"):
            d["translate"].pop("rows", None)
        os.makedirs(self._preset_dir(), exist_ok=True)
        path = os.path.join(self._preset_dir(), name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        self._refresh_presets()
        self.presets.setCurrentIndex(self.presets.findData(path))
        self.status.setText("Preset saved. On the command line: ovtsuite convert "
                            "<files> --preset \"%s\"" % path)

    def _load_preset(self):
        path = self.presets.currentData()
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Preset", str(exc))
            return
        self.apply_settings(d)

    def apply_settings(self, d: dict):
        t = d.get("target")
        if t:
            i = self.target.findData(t)
            if i >= 0:
                self.target.setCurrentIndex(i)
        self.size.setText("" if d.get("size") is None else "0x%X" % d["size"])
        self.base.setText("" if d.get("base") is None else "0x%X" % d["base"])
        i = self.dynamic.findData(d.get("dynamic", "max"))
        self.dynamic.setCurrentIndex(max(0, i))
        f = d.get("filters") or {}
        self.filter_box.setChecked(bool(f))
        self.pattern.setText(f.get("pattern", ""))
        self.categories.setText("; ".join(f.get("categories", [])))
        tr = d.get("translate") or {}
        self.tr_box.setChecked(bool(tr))
        if tr:
            self.tr_backend.setCurrentIndex(max(0, self.tr_backend.findData(tr.get("backend"))))
            self.tr_provider.setCurrentIndex(max(0, self.tr_provider.findData(tr.get("provider", ""))))
            self.tr_from.setCurrentText(tr.get("source", "auto"))
            self.tr_to.setCurrentText(tr.get("target", "en"))
            self.tr_mode.setCurrentIndex(max(0, self.tr_mode.findData(tr.get("mode", "replace"))))
        ck = d.get("checksum", "none")
        if not ck.startswith("dll:"):
            self.cks.setCurrentIndex(max(0, self.cks.findData(ck)))
        o = d.get("options") or {}
        self.indexed.setChecked(o.get("indexed", True))
        self.a2l_addr.setCurrentIndex(max(0, self.a2l_addr.findData(o.get("a2l_addresses", "offset"))))
        if o.get("record"):
            self.record.setValue(int(o["record"]))
        self.swap16.setChecked(bool(o.get("swap16")))
        self.fix_cks.setChecked(bool(o.get("checksum")))
        if "apply_checksum" in o:
            self.apply_cks.setChecked(bool(o["apply_checksum"]))
        self.port.setChecked(bool(o.get("port")))
        self.patch_split.setCurrentIndex(max(0, self.patch_split.findData(
            o.get("patch_split", "one"))))
        self.patch_outside.setCurrentIndex(max(0, self.patch_outside.findData(
            o.get("patch_outside", "keep"))))
        self.unverified.setCurrentIndex(max(0, self.unverified.findData(
            o.get("unverified", "category"))))
        self._want_rom = o.get("rom") or None
        if self._want_rom and self._rom_src is not None:
            i = self.rom.findData(self._want_rom)
            if i >= 0:
                self.rom.setCurrentIndex(i)
                self._want_rom = None

    def _save_state(self):
        try:
            d = self.job().to_dict()
            for k in ("sources", "out", "binary"):
                d.pop(k, None)
            d.get("options", {}).pop("reference", None)
            d.get("options", {}).pop("rom", None)          # belongs to one file
            d.get("options", {}).pop("patch_with", None)
            d.get("options", {}).pop("patch_title", None)
            if d.get("translate"):
                d["translate"].pop("rows", None)
            os.makedirs(T.config_dir(), exist_ok=True)
            with open(_cfg_path(_STATE), "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
        except (OSError, ValueError):
            pass

    def _restore_state(self):
        try:
            with open(_cfg_path(_STATE), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.apply_settings(d)

    # ---------------------------------------------------------- size
    _GEOM_KEY = "convert/geometry"
    _SPLIT_KEY = "convert/split"

    def _fit_window(self):
        """The size it was left at, else as tall as its settings need plus
        a preview -- never taller or wider than the screen."""
        from .appsettings import settings
        from .gui_fit import available, fit_to_screen
        st = settings()
        geo = st.value(self._GEOM_KEY)
        if geo is not None and self.restoreGeometry(geo):
            g = available(self)
            if g is not None and (self.width() > g.width()
                                  or self.height() > g.height()):
                fit_to_screen(self, self.width(), self.height(), 1.0, 0.95)
            spl = st.value(self._SPLIT_KEY)
            if spl is not None:
                self.split.restoreState(spl)
            return
        top = self._top.sizeHint().height()
        want_h = top + 300 + 90                 # preview + the rows under it
        fit_to_screen(self, 880, want_h, 0.9, 0.85)
        prev = max(160, self.height() - 90 - top)
        self.split.setSizes([max(120, self.height() - 90 - prev), prev])

    def _keep_size(self):
        from .appsettings import settings
        st = settings()
        st.setValue(self._GEOM_KEY, self.saveGeometry())
        st.setValue(self._SPLIT_KEY, self.split.saveState())

    def reject(self):
        self.close()                        # Esc closes as the title bar does

    def closeEvent(self, ev):
        self._keep_size()
        self.closeEvent_watch()
        w = self._worker
        if w is not None and not w.wait(10000):
            # still converting (a large port, an online translation): out of
            # sight until it is done, then closed -- a QThread must never be
            # destroyed while it runs
            self.hide()
            if not getattr(self, "_close_when_done", False):
                self._close_when_done = True
                w.finished.connect(self.close)
            ev.ignore()
            return
        super().closeEvent(ev)


def _fold_when_off(box: QtWidgets.QGroupBox) -> None:
    """A checkable group shows its fields only while it is ticked: the
    window stays short until an option is wanted."""
    def sync(on):
        for w in box.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            w.setVisible(on)
    box.toggled.connect(sync)
    sync(box.isChecked())


# ------------------------------------------------------------------ entries
# ------------------------------------------------------------- watch folder
_WATCH = "watch.json"


class _WatchThread(QtCore.QThread):
    event = QtCore.Signal(object)

    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        self.watcher = watcher
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                for e in self.watcher.poll():
                    self.event.emit(e)
                    if self._stop:
                        break
            except Exception as exc:                            # noqa: BLE001
                from .convert.watch import Event
                self.event.emit(Event("", False, "watch error: %s" % exc))
            for _ in range(10):
                if self._stop:
                    break
                self.msleep(100)


class WatchDialog(QtWidgets.QDialog):
    """Watch a folder: every file that arrives is converted with the
    converter window's settings, the listed files kept for every one."""

    def __init__(self, conv: "ConverterDialog"):
        super().__init__(conv)
        self.conv = conv
        self.setWindowTitle("Watch a folder")
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.resize(640, 480)
        self._thread: Optional[_WatchThread] = None
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.folder = QtWidgets.QLineEdit()
        self.folder.setPlaceholderText("the folder files arrive in")
        form.addRow("Watch:", self._with_browse(self.folder, "Folder to watch"))
        self.out_dir = QtWidgets.QLineEdit()
        self.out_dir.setPlaceholderText("default: a \"Converted\" folder inside it")
        form.addRow("Results to:", self._with_browse(self.out_dir, "Output folder"))
        self.after = QtWidgets.QComboBox()
        self.after.addItem("Leave the file where it is", "leave")
        self.after.addItem("Move the file to a \"Done\" folder inside it", "move")
        form.addRow("After converting:", self.after)
        self.existing = QtWidgets.QCheckBox("Also convert the files already there")
        self.subfolders = QtWidgets.QCheckBox("Include the folders inside it")
        form.addRow(self.existing)
        form.addRow(self.subfolders)
        lay.addLayout(form)
        self.what = QtWidgets.QLabel()
        self.what.setWordWrap(True)
        self.what.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        lay.addWidget(self.what)
        self.log = QtWidgets.QListWidget()
        self.log.setToolTip("Double-click a result to open its folder")
        self.log.itemDoubleClicked.connect(self._open_item)
        lay.addWidget(self.log, 1)
        row = QtWidgets.QHBoxLayout()
        self.state = QtWidgets.QLabel("Not watching.")
        row.addWidget(self.state, 1)
        self.b_start = QtWidgets.QPushButton("Start watching")
        self.b_start.setDefault(True)
        self.b_start.clicked.connect(self.toggle)
        row.addWidget(self.b_start)
        b_close = QtWidgets.QPushButton("Close")
        b_close.clicked.connect(self.close)
        row.addWidget(b_close)
        lay.addLayout(row)
        self._restore()

    def _with_browse(self, edit, title):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QtWidgets.QPushButton("Browse...")

        def pick():
            d = QtWidgets.QFileDialog.getExistingDirectory(self, title, edit.text())
            if d:
                edit.setText(d)
        b.clicked.connect(pick)
        h.addWidget(b)
        return w

    def refresh_job(self):
        """Say what each arriving file will do, from the window's settings."""
        from .convert.watch import roles
        if self.running():
            return
        j = self.conv.watch_job()
        fixed = {k for k, _t in self.conv._kinds.values()}
        if not j.target:
            self.what.setText("Choose what to convert to in the converter window first.")
            return
        r = roles(j, fixed)
        lines = ["With the converter window's settings (%s):" % C.NAMES.get(j.target, j.target)]
        if j.sources:
            lines.append("Kept for every file: " + ", ".join(
                os.path.basename(p) for p in j.sources))
        if j.binary:
            lines.append("Binary: " + os.path.basename(j.binary))
        for k, word in (("definition", "A definition"), ("values", "A value file"),
                        ("image", "A binary")):
            lines.append("  %s that arrives: %s" % (word, r[k]))
        self.what.setText("\n".join(lines))

    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def toggle(self):
        if self.running():
            self.stop()
            return
        from .convert.watch import Watcher
        folder = self.folder.text().strip()
        if not folder or not os.path.isdir(folder):
            QtWidgets.QMessageBox.information(self, "Watch a folder",
                                              "Choose the folder to watch.")
            return
        try:
            w = Watcher(folder, self.conv.watch_job(),
                        out_dir=self.out_dir.text().strip() or None,
                        after=self.after.currentData(), existing=self.existing.isChecked(),
                        recursive=self.subfolders.isChecked())
        except (C.ConvertError, ValueError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, "Watch a folder", str(exc))
            return
        self._save()
        self._thread = _WatchThread(w, self)
        self._thread.event.connect(self._event)
        self._thread.start()
        self._set_running(True, w)

    def stop(self):
        t = self._thread
        if t is not None:
            t.stop()
            if not t.wait(15000):
                # a conversion is still running: keep the thread (and the
                # button off) until it ends, or a second watcher could start
                self.b_start.setEnabled(False)
                self.state.setText("Stopping: finishing the file being converted...")
                t.finished.connect(self._stopped)
                return
            self._thread = None
        self._set_running(False)

    def _stopped(self):
        self._thread = None
        self.b_start.setEnabled(True)
        self._set_running(False)

    def _set_running(self, on: bool, w=None):
        self.b_start.setText("Stop watching" if on else "Start watching")
        for wd in (self.folder, self.out_dir, self.after, self.existing, self.subfolders):
            wd.setEnabled(not on)
        self.state.setText("Watching %s -> %s" % (w.folder, w.out_dir) if on and w
                           else "Not watching.")
        if not on:
            self.refresh_job()

    def _event(self, e):
        mark = {True: "\u2713", False: "\u2717", None: "\u2013"}[e.ok]
        name = os.path.basename(e.path) if e.path else ""
        it = QtWidgets.QListWidgetItem("%s  %s  %s: %s" % (
            time.strftime("%H:%M:%S"), mark, name, e.text))
        it.setData(QtCore.Qt.ItemDataRole.UserRole, e.out[0] if e.out else e.path)
        if e.ok is False:
            it.setForeground(QtGui.QBrush(QtGui.QColor("#c0392b")))
        elif e.ok is None:
            it.setForeground(QtGui.QBrush(QtGui.QColor("#888888")))
        self.log.insertItem(0, it)
        while self.log.count() > 500:
            self.log.takeItem(self.log.count() - 1)

    def _open_item(self, it):
        p = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if p:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(
                os.path.dirname(os.path.abspath(p))))

    def _save(self):
        d = {"folder": self.folder.text().strip(), "out": self.out_dir.text().strip(),
             "after": self.after.currentData(), "existing": self.existing.isChecked(),
             "subfolders": self.subfolders.isChecked()}
        try:
            os.makedirs(T.config_dir(), exist_ok=True)
            with open(_cfg_path(_WATCH), "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
        except OSError:
            pass

    def _restore(self):
        try:
            with open(_cfg_path(_WATCH), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.folder.setText(d.get("folder", ""))
        self.out_dir.setText(d.get("out", ""))
        self.after.setCurrentIndex(max(0, self.after.findData(d.get("after", "leave"))))
        self.existing.setChecked(bool(d.get("existing")))
        self.subfolders.setChecked(bool(d.get("subfolders")))

    def closeEvent(self, ev):
        if self.running():
            r = QtWidgets.QMessageBox.question(
                self, "Watch a folder", "Stop watching the folder?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.stop()
        super().closeEvent(ev)


def open_in_app(win) -> ConverterDialog:
    """Tools > Converter... in the main window: non-modal, one at a time,
    with the open tab's binary offered as the optional binary."""
    dlg = getattr(win, "_convert_dlg", None)
    if dlg is not None and dlg.isVisible():
        dlg.raise_()
        dlg.activateWindow()
        return dlg
    binary = ""
    try:
        name = win.primary.binfile.name
        if name and os.path.isfile(name):
            binary = name
    except Exception:
        pass
    dlg = ConverterDialog(win, binary=binary)
    dlg.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    win._convert_dlg = dlg
    dlg.show()
    return dlg


def run_converter(paths=()) -> int:
    """``ovtsuite --converter [file ...]``: the converter alone, no main
    window (the OVT Converter shortcut, files dropped on it, the right-click
    menu). Files given while a converter window is open go to that window, so
    several files right-clicked together -- one start each -- all land in
    one window."""
    import sys
    from .gui import _run_tool
    from . import gui_instance
    files = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    name = gui_instance.server_name("converter")
    lock = QtCore.QLockFile(os.path.join(QtCore.QDir.tempPath(), name + ".lock"))
    lock.setStaleLockTime(30000)
    if files:
        if gui_instance.hand_over(files, name=name):
            return 0
        if not lock.tryLock(0):
            # another start of the same right-click is opening the window:
            # hand over once it listens
            t = QtCore.QElapsedTimer()
            t.start()
            while t.elapsed() < 20000:
                QtCore.QThread.msleep(200)
                if gui_instance.hand_over(files, name=name):
                    return 0
                if lock.tryLock(0):
                    break
    else:
        lock.tryLock(0)

    def make():
        dlg = ConverterDialog(None, sources=files)
        gui_instance.listen(dlg, lambda p: dlg.add_sources([p]), name=name)
        lock.unlock()
        return dlg
    try:
        return _run_tool("converter", branding.CONVERTER_APP_ID, make)
    finally:
        if lock.isLocked():
            lock.unlock()
