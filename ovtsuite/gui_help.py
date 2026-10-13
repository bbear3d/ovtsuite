# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The help window, and F1 anywhere in the app.

One window per process (Help > Contents, F1, a tool's Help button): contents
on the left -- the base app's pages and one section per installed module --
the page on the right, Back / Forward / Home, search, and "Open online" when a
website is configured (data/help/help.json). Pages are the Markdown files
:mod:`ovtsuite.helpdocs` collects, shown by Qt's own Markdown support, so the
help needs no web engine.

F1 opens the page whose ``context`` names the most specific class around the
keyboard focus: the focused widget and each widget it sits in, up to and
including the active window (each with its base classes) -- so a docked panel
such as the Find panel gets its own page -- otherwise the contents page.
"""

import os
from typing import List, Optional

import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from . import helpdocs as _docs

_window = None          # the one help window
_filter = None          # the one F1 filter

#: Qt's Markdown rendering is plain; this makes headings, tables and code read
#: like the website. Applied to the HTML Qt produces from the Markdown.
_CSS = """
body { line-height: 135%%; }
h1 { font-size: 20pt; margin-top: 4px; margin-bottom: 10px; }
h2 { font-size: 15pt; margin-top: 18px; margin-bottom: 6px; }
h3 { font-size: 12pt; margin-top: 14px; margin-bottom: 4px; }
p, li { margin-top: 3px; margin-bottom: 5px; }
code { font-family: monospace; }
pre { font-family: monospace; background-color: %(alt)s; padding: 6px; }
table { border-collapse: collapse; margin-top: 6px; margin-bottom: 8px; }
th { background-color: %(alt)s; font-weight: bold; padding: 4px 8px; }
td { padding: 4px 8px; }
blockquote { margin-left: 12px; margin-right: 12px; }
"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _title() -> str:
    return "%s Help" % branding.APP_NAME


class _Browser(QtWidgets.QTextBrowser):
    """A text browser that serves the smoothly scaled pictures."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fitted = {}

    def loadResource(self, kind, url):
        img = self.fitted.get(url.toString())
        if img is not None:
            return img
        return super().loadResource(kind, url)


class HelpWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        self.setWindowTitle(_title())
        self.setWindowIcon(QtWidgets.QApplication.windowIcon())
        self.resize(1000, 700)
        self.index = _docs.HelpIndex()
        self.current: Optional[str] = None
        self._back: List[str] = []
        self._fwd: List[str] = []

        lay = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        st = self.style()
        self.back_btn = self._tool(bar, st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ArrowBack),
                                   "Back (Alt+Left)", self.go_back)
        self.fwd_btn = self._tool(bar, st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ArrowForward),
                                  "Forward (Alt+Right)", self.go_forward)
        self._tool(bar, st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirHomeIcon),
                   "Contents", self.go_home)
        bar.addSpacing(8)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search help")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        self.search.returnPressed.connect(self._open_first_hit)
        bar.addWidget(self.search, 1)
        self.online_btn = QtWidgets.QPushButton("Open online")
        self.online_btn.setToolTip("This page on the website, in your browser")
        self.online_btn.clicked.connect(self._open_online)
        self.online_btn.setVisible(bool(_docs.online_url()))
        bar.addWidget(self.online_btn)
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.side = QtWidgets.QStackedWidget()
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemActivated.connect(self._tree_open)
        self.tree.itemClicked.connect(self._tree_open)
        self.side.addWidget(self.tree)
        self.hits = QtWidgets.QListWidget()
        self.hits.setWordWrap(True)
        self.hits.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hits.itemActivated.connect(self._hit_open)
        self.hits.itemClicked.connect(self._hit_open)
        self.side.addWidget(self.hits)
        split.addWidget(self.side)

        self.view = _Browser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)
        self.view.document().setDocumentMargin(14)
        split.addWidget(self.view)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 740])
        lay.addWidget(split, 1)

        for seq, slot in ((QtGui.QKeySequence.StandardKey.Back, self.go_back),
                          (QtGui.QKeySequence.StandardKey.Forward, self.go_forward),
                          (QtGui.QKeySequence.StandardKey.Find, self.search.setFocus)):
            QtGui.QShortcut(QtGui.QKeySequence(seq), self, activated=slot)
        self._items = {}
        self._fill_tree()
        self._update_nav()

    def _tool(self, bar, icon, tip, slot):
        b = QtWidgets.QToolButton()
        b.setIcon(icon)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        bar.addWidget(b)
        return b

    # -- contents -----------------------------------------------------------
    def _fill_tree(self):
        self.tree.clear()
        self._items = {}

        def add(parent, sec):
            it = QtWidgets.QTreeWidgetItem([sec.title])
            if sec.page is not None:
                it.setData(0, QtCore.Qt.ItemDataRole.UserRole, sec.page.id)
                self._items[sec.page.id] = it
            else:
                f = it.font(0)
                f.setBold(True)
                it.setFont(0, f)
            (parent.addChild if parent is not None else self.tree.addTopLevelItem)(it)
            for c in sec.children:
                add(it, c)
            return it

        for sec in self.index.toc():
            add(None, sec).setExpanded(True)

    def _tree_open(self, item, _col=0):
        pid = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if pid:
            self.show_page(pid)
        else:
            item.setExpanded(not item.isExpanded())

    # -- pages --------------------------------------------------------------
    def show_page(self, pid: Optional[str], remember: bool = True):
        if not pid:
            home = self.index.home()
            if home is None:
                self.view.setMarkdown("# %s\n\nNo help pages are installed." % _title())
                return
            pid = home.id
        if remember and self.current and self.current != pid:
            self._back.append(self.current)
            self._fwd.clear()
        self.current = pid
        page = self.index.get(pid)
        if page is None:
            text, base = (self.index.missing_text(pid) if self.index.module_of(pid)
                          else "# Page not found\n\nThere is no help page `%s`." % pid), ""
            self.setWindowTitle(_title())
        else:
            text, base = page.text, page.dir
            self.setWindowTitle("%s — %s" % (page.title, _title()))
        self._render(text, base)
        it = self._items.get(pid)
        if it is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(it)
            self.tree.blockSignals(False)
        self._update_nav()

    def _render(self, markdown: str, base_dir: str, keep_pos: bool = False):
        doc = QtGui.QTextDocument()
        doc.setMarkdown(markdown)
        html = doc.toHtml()
        vd = self.view.document()
        pal = self.view.palette()
        vd.setDefaultStyleSheet(_CSS % {"alt": pal.alternateBase().color().name()})
        if base_dir:
            vd.setBaseUrl(QtCore.QUrl.fromLocalFile(base_dir.rstrip("/\\") + "/"))
            self.view.setSearchPaths([base_dir])
        pos = self.view.verticalScrollBar().value()
        self.view.setHtml(self._fit_images(html, base_dir))
        if keep_pos:
            self.view.verticalScrollBar().setValue(pos)
        else:
            self.view.moveCursor(QtGui.QTextCursor.MoveOperation.Start)
        self._shown = (markdown, base_dir)

    def _fit_images(self, html: str, base_dir: str) -> str:
        """Scale screenshots down to the page width (never up), smoothly --
        the text browser's own scaling is nearest-neighbour and unreadable.
        A scaled picture links to the full-size file (it opens in the
        system's image viewer)."""
        import re
        avail = max(200, self.view.viewport().width() - 40)
        self.view.fitted = {}

        def fit(m):
            src = m.group(1)
            path = src if os.path.isabs(src) else os.path.join(base_dir, src)
            img = QtGui.QImage(path)
            if img.isNull() or img.width() <= avail:
                return m.group(0)
            small = img.scaledToWidth(avail, QtCore.Qt.TransformationMode.SmoothTransformation)
            name = "fitted:%d:%s" % (avail, src)
            self.view.fitted[name] = small
            return '<a href="%s"><img src="%s"' % (src, name)
        out = re.sub(r'<img src="([^"]+)"', fit, html)
        return re.sub(r'(<a href="[^"]+"><img [^>]*>)', r"\1</a>", out)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, "_shown", None) and not getattr(self, "_refit", None):
            self._refit = QtCore.QTimer(self)
            self._refit.setSingleShot(True)
            self._refit.timeout.connect(lambda: self._render(*self._shown, keep_pos=True))
        if getattr(self, "_refit", None):
            self._refit.start(150)

    def _link(self, url: QtCore.QUrl):
        href = url.toString()
        kind, target = self.index.resolve(self.current or "index", href)
        if kind == "external":
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(target))
        elif kind == "page":
            self.show_page(target)
        elif kind == "file":
            if os.path.exists(target):
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(target))
        else:                                     # an anchor on this page
            if target:
                self.view.scrollToAnchor(target)

    # -- navigation ---------------------------------------------------------
    def go_back(self):
        if self._back:
            self._fwd.append(self.current)
            self.show_page(self._back.pop(), remember=False)

    def go_forward(self):
        if self._fwd:
            self._back.append(self.current)
            self.show_page(self._fwd.pop(), remember=False)

    def go_home(self):
        self.search.clear()
        self.show_page(None)

    def _update_nav(self):
        self.back_btn.setEnabled(bool(self._back))
        self.fwd_btn.setEnabled(bool(self._fwd))
        self.online_btn.setEnabled(bool(self.current))

    def _open_online(self):
        url = _docs.online_url(self.current or "")
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    # -- search -------------------------------------------------------------
    def _on_search(self, text):
        if not text.strip():
            self.side.setCurrentWidget(self.tree)
            return
        self.hits.clear()
        found = self.index.search(text)
        grey = self.palette().placeholderText().color().name()
        width = max(120, self.side.width() - 24)
        for page, snippet in found:
            it = QtWidgets.QListWidgetItem()
            it.setData(QtCore.Qt.ItemDataRole.UserRole, page.id)
            lab = QtWidgets.QLabel("<b>%s</b><br><span style='color:%s'>%s</span>"
                                   % (_esc(page.title), grey, _esc(snippet)))
            lab.setWordWrap(True)
            lab.setContentsMargins(4, 3, 4, 3)
            lab.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lab.setFixedWidth(width)
            lab.adjustSize()
            it.setSizeHint(QtCore.QSize(width, lab.sizeHint().height()))
            self.hits.addItem(it)
            self.hits.setItemWidget(it, lab)
        if not found:
            it = QtWidgets.QListWidgetItem("No page mentions that.")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.hits.addItem(it)
        self.side.setCurrentWidget(self.hits)

    def _hit_open(self, item):
        pid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if pid:
            self.show_page(pid)
            words = self.search.text().split()
            if words:
                self.view.find(words[0])

    def _open_first_hit(self):
        if self.side.currentWidget() is self.hits and self.hits.count():
            self._hit_open(self.hits.item(0))


def show_help(parent=None, topic: Optional[str] = None, context=None):
    """Open (or raise) the help window at page id ``topic``, or at the page
    documenting ``context`` -- a widget or class -- or at the contents."""
    win = _window_for(QtWidgets.QApplication.activeModalWidget())
    pid = topic
    if pid is None and context is not None:
        page = win.index.for_context(_class_names(context))
        pid = page.id if page is not None else None
    win.show_page(pid)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def _window_for(modal) -> "HelpWindow":
    """The help window to use. While a modal dialog runs -- the standalone
    flasher, logger and sniffer are one, as are Options, the flasher in the
    main window, ... -- Qt blocks every window outside that dialog: the
    shared help window would open behind it and could not be used. So help
    asked for then is a window of the dialog's own (never blocked by it),
    which closes with the dialog."""
    global _window
    if modal is not None and not isinstance(modal, HelpWindow):
        own = getattr(modal, "_ovt_help_window", None)
        if own is None or not shiboken6.isValid(own):
            own = HelpWindow(modal)
            modal._ovt_help_window = own
        return own
    if _window is None or not shiboken6.isValid(_window):
        _window = HelpWindow(None)
    return _window


def _class_names(context) -> List[str]:
    """Class names to look a page up by, most specific first. ``context`` is a
    class, a widget, or a list of them (most specific first); each
    contributes its class and then its base classes."""
    items = context if isinstance(context, (list, tuple)) else [context]
    names: List[str] = []
    for it in items:
        if it is None:
            continue
        cls = it if isinstance(it, type) else type(it)
        for c in cls.__mro__:
            if c.__name__ not in names:
                names.append(c.__name__)
    return names


def focus_chain(window) -> list:
    """The widgets F1 asks about, most specific first: the focus widget and
    every widget it sits in, up to and including ``window`` (the active
    window or modal dialog). Just ``[window]`` when the focus is elsewhere."""
    chain = []
    fw = QtWidgets.QApplication.focusWidget()
    if fw is not None and window is not None and (
            fw is window or window.isAncestorOf(fw)):
        while fw is not None and fw is not window:
            chain.append(fw)
            fw = fw.parentWidget()
    chain.append(window)
    return chain


class _F1Filter(QtCore.QObject):
    """F1 anywhere: help for what has the focus -- the innermost widget with a
    page (a docked panel, say), else the window in front."""

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.KeyPress and ev.key() == QtCore.Qt.Key.Key_F1 \
                and not ev.modifiers():
            w = (QtWidgets.QApplication.activeModalWidget()
                 or QtWidgets.QApplication.activeWindow())
            if isinstance(w, HelpWindow):
                return False
            show_help(None, context=focus_chain(w))
            return True
        return False


def install_f1(app=None):
    """Make F1 open context help across the application (once)."""
    global _filter
    app = app or QtWidgets.QApplication.instance()
    if app is None or _filter is not None:
        return
    _filter = _F1Filter(app)
    app.installEventFilter(_filter)
