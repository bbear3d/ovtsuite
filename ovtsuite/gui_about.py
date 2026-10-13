# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Help > About: the version, the installed modules, and the credits.

Two tabs. **About** -- what the app is, its version and the versions it runs on
(one click copies them for a bug report), the installed modules, the
disclaimer. **Credits & licences** -- every incorporated project, runtime
component and module-supplied patch (:mod:`ovtsuite.credits`), grouped; picking
one shows who made it, what it is used for, and its full licence text.

"Save notices..." writes the same document the build ships as
THIRD-PARTY-NOTICES.txt.
"""

import os
import platform
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from . import credits as _credits

ABOUT, CREDITS = 0, 1


def runtime_info() -> str:
    """One line per runtime fact, for the About tab and the clipboard."""
    try:
        import PySide6
        pyside = PySide6.__version__
    except Exception:            # pragma: no cover
        pyside = "?"
    bits = "64-bit" if sys.maxsize > 2 ** 32 else "32-bit"
    frozen = "installed build" if getattr(sys, "frozen", False) else "running from source"
    return "\n".join([
        "%s %s (%s)" % (branding.APP_NAME, branding.__version__, frozen),
        "Python %s, %s" % (platform.python_version(), bits),
        "Qt %s, PySide6 %s" % (QtCore.qVersion(), pyside),
        "%s %s" % (platform.system(), platform.release()),
    ])


def _modules():
    """[(name, version, variant)] of the installed modules."""
    try:
        from .modules import api as _api
        from .modules import registry as _registry
        out = []
        for mid, spec in sorted(_registry.installed().items()):
            variant = "checksum only" if spec.variant == _api.CHECKSUM_ONLY else ""
            out.append((spec.name, spec.version, variant))
        return out
    except Exception:
        return []


def _icon():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                     "%s.ico" % branding.PKG)
    return QtGui.QIcon(p) if os.path.isfile(p) else QtGui.QIcon()


class AboutDialog(QtWidgets.QDialog):
    """``tool`` (``"flasher"``, ``"sniffer"`` ...) titles it for a standalone
    tool: the tool's name and icon, "part of" the suite."""

    def __init__(self, parent=None, tab: int = ABOUT, tool: str = ""):
        super().__init__(parent)
        self.tool_name = branding.TOOL_NAMES.get(tool, "") if tool else ""
        self.setWindowTitle("About %s" % (self.tool_name or branding.APP_NAME))
        self.setWindowIcon(self._icon())
        self.resize(900, 620)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(self._header())
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._about_tab(), "About")
        self.tabs.addTab(self._credits_tab(), "Credits && licences")
        lay.addWidget(self.tabs, 1)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self.about_qt_btn = bb.addButton(
            "About Qt", QtWidgets.QDialogButtonBox.ButtonRole.HelpRole)
        self.about_qt_btn.clicked.connect(lambda: QtWidgets.QMessageBox.aboutQt(self))
        self.save_btn = bb.addButton(
            "Save notices...", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.save_btn.setToolTip("Save every credit and licence text to one text file")
        self.save_btn.clicked.connect(self._save_notices)
        lay.addWidget(bb)
        self.tabs.setCurrentIndex(tab)

    def _icon(self):
        if self.tool_name:
            ic = QtWidgets.QApplication.windowIcon()     # the tool's (gui.use_tool_icon)
            if not ic.isNull():
                return ic
        return _icon()

    # -- header -------------------------------------------------------------
    def _header(self):
        row = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel()
        pm = self._icon().pixmap(64, 64)
        if not pm.isNull():
            icon.setPixmap(pm)
        icon.setFixedSize(72, 72)
        icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        row.addWidget(icon)
        col = QtWidgets.QVBoxLayout()
        name = QtWidgets.QLabel(self.tool_name or branding.APP_NAME)
        f = name.font()
        f.setPointSizeF(f.pointSizeF() * 1.8)
        f.setBold(True)
        name.setFont(f)
        col.addWidget(name)
        if self.tool_name:
            sub = QtWidgets.QLabel("Part of %s (%s)  \u00b7  version %s"
                                   % (branding.APP_NAME, branding.APP_LONG, branding.__version__))
        else:
            sub = QtWidgets.QLabel("%s  \u00b7  version %s"
                                   % (branding.APP_LONG, branding.__version__))
        col.addWidget(sub)
        cr = QtWidgets.QLabel(_credits.copyright_line())
        cr.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        col.addWidget(cr)
        lic = QtWidgets.QLabel(_credits.license_line())
        lic.setWordWrap(True)
        lic.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        lic.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        col.addWidget(lic)
        row.addLayout(col, 1)
        return row

    # -- About tab ----------------------------------------------------------
    def _about_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        desc = QtWidgets.QLabel(branding.APP_DESC + ".")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        g = QtWidgets.QGroupBox("Version")
        gl = QtWidgets.QHBoxLayout(g)
        self.info_label = QtWidgets.QLabel(runtime_info())
        self.info_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        gl.addWidget(self.info_label, 1)
        copy = QtWidgets.QPushButton("Copy")
        copy.setToolTip("Copy these versions, e.g. for a bug report")
        copy.clicked.connect(self._copy_info)
        gl.addWidget(copy, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        lay.addWidget(g)

        g = QtWidgets.QGroupBox("Installed modules")
        gl = QtWidgets.QVBoxLayout(g)
        mods = _modules()
        if mods:
            t = QtWidgets.QTreeWidget()
            t.setRootIsDecorated(False)
            t.setHeaderLabels(["Module", "Version", ""])
            for n, v, var in mods:
                t.addTopLevelItem(QtWidgets.QTreeWidgetItem([n, v, var]))
            for i in range(3):
                t.resizeColumnToContents(i)
            t.setMaximumHeight(150)
            self.modules_view = t
            gl.addWidget(t)
        else:
            gl.addWidget(QtWidgets.QLabel(
                "No modules installed (Tools > Modules... installs one)."))
        lay.addWidget(g)

        disc = QtWidgets.QLabel(_credits.DISCLAIMER)
        disc.setWordWrap(True)
        disc.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        lay.addWidget(disc)
        lay.addStretch(1)
        return w

    def _copy_info(self):
        mods = "\n".join("  %s %s %s" % m for m in _modules())
        QtWidgets.QApplication.clipboard().setText(
            runtime_info() + ("\nModules:\n" + mods if mods else ""))

    # -- Credits tab --------------------------------------------------------
    def _credits_tab(self):
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Component", "Licence"])
        self.tree.setUniformRowHeights(True)
        self._items = {}
        self.credit_list = _credits.all_credits()
        bold = self.tree.font()
        bold.setBold(True)
        first = None
        for group in _credits.GROUPS:
            rows = [c for c in self.credit_list if c.group == group]
            if not rows:
                continue
            top = QtWidgets.QTreeWidgetItem([group, ""])
            top.setFont(0, bold)
            top.setFlags(top.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            for c in rows:
                it = QtWidgets.QTreeWidgetItem([c.name, c.license or "—"])
                it.setToolTip(0, c.what)
                top.addChild(it)
                self._items[id(it)] = c
                first = first or it
            top.setExpanded(True)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 150)
        self.tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.tree.currentItemChanged.connect(self._show)
        split.addWidget(self.tree)

        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.detail.setOpenExternalLinks(True)
        self.detail.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        self.detail.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        rl.addWidget(self.detail)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        self.text.setFont(mono)
        rl.addWidget(self.text, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([400, 500])
        if first is not None:
            self.tree.setCurrentItem(first)
        return split

    def _show(self, item, _prev=None):
        c = self._items.get(id(item)) if item is not None else None
        if c is None:
            return
        esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")   # noqa: E731
                         .replace(">", "&gt;"))
        parts = ["<b>%s</b>" % esc(c.name), esc(c.what)]
        if c.copyright:
            parts.append(esc(c.copyright))
        parts.append("Licence: %s" % esc(c.license or "none (acknowledgement)"))
        if c.where and c.where != "base":
            try:
                from .modules import registry as _registry
                spec = _registry.spec(c.where)
                mod = spec.name if spec is not None else c.where
            except Exception:
                mod = c.where
            parts.append("Comes with the %s module" % esc(mod))
        if c.url:
            parts.append('<a href="%s">%s</a>' % (esc(c.url), esc(c.url)))
        if c.note:
            parts.append("<i>%s</i>" % esc(c.note))
        self.detail.setText("<br>".join(parts))
        self.text.setPlainText(_credits.license_text(c))
        self.text.moveCursor(QtGui.QTextCursor.MoveOperation.Start)

    def _save_notices(self):
        name = "THIRD-PARTY-NOTICES.txt"
        start = os.path.join(QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation), name)
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save notices", start, "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            _credits.write_notices(path)
        except OSError as ex:
            QtWidgets.QMessageBox.warning(self, "Save notices", str(ex))


def show_about(parent=None, tab: int = ABOUT, tool: str = ""):
    AboutDialog(parent, tab, tool).exec()


# ------------------------------------------------------------ standalone tools
def _find_in_layout(layout, widget):
    """``(layout, index)`` of ``widget`` anywhere under ``layout``, or None."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is widget:
            return layout, i
        sub = item.layout()
        if sub is not None:
            hit = _find_in_layout(sub, widget)
            if hit:
                return hit
    return None


def _place(dlg, btn):
    """Put ``btn`` with the dialog's own closing buttons: in its button box,
    else just before its Close button, else on a row of its own at the bottom
    right (shared by every button added here, in the order added)."""
    boxes = [b for b in dlg.findChildren(QtWidgets.QDialogButtonBox) if b.window() is dlg]
    if boxes:
        boxes[-1].addButton(btn, QtWidgets.QDialogButtonBox.ButtonRole.HelpRole)
        return
    lay = dlg.layout()
    closes = [b for b in dlg.findChildren(QtWidgets.QPushButton)
              if b.window() is dlg and b.text().replace("&", "").strip() == "Close"]
    if lay is not None and closes:
        hit = _find_in_layout(lay, closes[-1])
        if hit and isinstance(hit[0], QtWidgets.QBoxLayout):
            hit[0].insertWidget(hit[1], btn)
            return
    row = getattr(dlg, "_tool_button_row", None)
    if row is None:
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        if isinstance(lay, QtWidgets.QGridLayout):
            lay.addLayout(row, lay.rowCount(), 0, 1, max(1, lay.columnCount()))
        elif isinstance(lay, QtWidgets.QBoxLayout):
            lay.addLayout(row)
        elif lay is None:
            QtWidgets.QVBoxLayout(dlg).addLayout(row)
        else:                                 # a form or custom layout
            btn.setParent(dlg)
            return
        dlg._tool_button_row = row
    row.addWidget(btn)


def _button(text, tip, slot):
    btn = QtWidgets.QPushButton(text)
    btn.setAutoDefault(False)
    btn.setToolTip(tip)
    btn.clicked.connect(slot)
    return btn


def add_about_button(dlg, tool: str = ""):
    """An "About..." button for a standalone tool's dialog (Flasher, Sniffer,
    Logger, Simulator), placed with its closing buttons. Returns it."""
    btn = _button("About...", "Version, credits and licences",
                  lambda: show_about(dlg, ABOUT, tool))
    btn.setObjectName("aboutButton")
    _place(dlg, btn)
    return btn


def add_help_button(dlg):
    """A "Help" button (same as F1): the help page for this dialog."""
    def go():
        from .gui_help import show_help
        show_help(dlg, context=dlg)
    btn = _button("Help", "Help for this tool (F1)", go)
    btn.setObjectName("helpButton")
    _place(dlg, btn)
    return btn


def add_tool_buttons(dlg, tool: str = ""):
    """Help and About for a standalone tool; returns ``(help, about)``."""
    return add_help_button(dlg), add_about_button(dlg, tool)
