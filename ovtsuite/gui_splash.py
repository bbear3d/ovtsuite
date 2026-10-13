# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The start-up splash: the app's icon, name and version, and what it is
doing ("Loading modules...", "Reopening A6 BHW swap (1 of 3)...") over a thin
progress bar.

It is a plain frameless window drawn with the app's palette, so it follows
the theme (shown after Options > Appearance > Theme is applied). Start-up
drives it through module-level calls that do nothing when there is no
splash -- so code that reports progress (reopening last session's projects)
need not know whether one is up:

* :func:`begin` -- show it (the main app, or a standalone tool by name),
  once whatever start-up asks the user has been answered;
* :func:`step` -- say what is happening now, optionally how far along (0..1);
* :func:`end` -- close it, once everything has loaded.

It is shown the whole time in between, never hidden and shown again. A
message that comes up while it is there (a damaged project, a recovery
warning) is put on top of it, so nothing waits behind the splash.
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding

_splash: Optional["Splash"] = None


class Splash(QtWidgets.QWidget):
    WIDTH, HEIGHT = 460, 250

    def __init__(self, title: str, icon: QtGui.QIcon):
        super().__init__(None, QtCore.Qt.WindowType.SplashScreen
                         | QtCore.Qt.WindowType.FramelessWindowHint
                         | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setAutoFillBackground(True)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(24, 30, 24, 18)
        v.setSpacing(6)
        v.addStretch(1)
        self.icon = QtWidgets.QLabel()
        self.icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        dpr = self.devicePixelRatioF()
        pm = icon.pixmap(QtCore.QSize(64, 64), dpr)
        self.icon.setPixmap(pm)
        v.addWidget(self.icon)
        self.name = QtWidgets.QLabel(title)
        self.name.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        f = self.name.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        self.name.setFont(f)
        v.addWidget(self.name)
        self.sub = QtWidgets.QLabel("%s · %s" % (branding.APP_LONG,
                                                 branding.__version__))
        self.sub.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        _muted(self.sub)
        v.addWidget(self.sub)
        v.addStretch(1)
        self.status = QtWidgets.QLabel("Starting...")
        _muted(self.status, small=True)
        v.addWidget(self.status)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setRange(0, 1000)
        self.bar.setValue(30)
        v.addWidget(self.bar)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QtGui.QPainter(self)
        p.setPen(self.palette().color(QtGui.QPalette.ColorRole.Mid))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))     # a frameless edge
        p.end()

    def place(self, screen=None):
        """In the middle of ``screen`` -- the one the app starts on, so the
        splash and the window share a screen of a multi-screen desk."""
        scr = screen or QtGui.QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.center() - self.rect().center())


class _DialogWatch(QtCore.QObject):
    """A dialog that comes up while the splash is there (a damaged project,
    a recovery warning ...) goes on top of it: the splash stays on top of
    the windows, and a question must not wait behind it. (The flag is set on
    the dialog's native window: setting it on the widget would hide and
    show the dialog again.)"""

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Show and \
                isinstance(obj, QtWidgets.QProgressDialog):
            _below_splash(obj)          # "Opening ...": beside it, not over it
        elif ev.type() == QtCore.QEvent.Type.Show and \
                isinstance(obj, QtWidgets.QDialog):
            h = obj.windowHandle()
            if h is not None:
                h.setFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
            obj.raise_()
            obj.activateWindow()
        return False


def _below_splash(w) -> None:
    """Put a progress window (a file being opened) just under the splash,
    centred on it -- or above it when there is no room below -- so it never
    covers the splash."""
    s = _splash
    if s is None or not s.isVisible():
        return
    w.adjustSize()
    sg = s.frameGeometry()
    scr = s.screen() or QtGui.QGuiApplication.primaryScreen()
    room = scr.availableGeometry() if scr is not None else None
    x = sg.center().x() - w.frameGeometry().width() // 2
    y = sg.bottom() + 12
    if room is not None and y + w.frameGeometry().height() > room.bottom():
        y = sg.top() - 12 - w.frameGeometry().height()
    w.move(x, y)


def _muted(label, small=False):
    pal = label.palette()
    pal.setColor(QtGui.QPalette.ColorRole.WindowText,
                 pal.color(QtGui.QPalette.ColorGroup.Disabled,
                           QtGui.QPalette.ColorRole.WindowText))
    label.setPalette(pal)
    if small:
        f = label.font()
        f.setPointSizeF(f.pointSizeF() * 0.92)
        label.setFont(f)


def _pump():
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


def begin(tool: str = "", screen=None) -> Optional[Splash]:
    """Show the splash for the main app, or for standalone tool ``tool``
    (``"flasher"``, ``"logger"`` ...), on ``screen``. Call after the theme
    and the app icon are set, and after anything start-up asks the user."""
    global _splash
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    from .gui import app_icon
    title = branding.TOOL_NAMES.get(tool, branding.APP_NAME)
    _splash = Splash(title, app_icon())
    _splash._watch = _DialogWatch(_splash)
    app.installEventFilter(_splash._watch)
    _splash.place(screen)
    _splash.show()
    _splash.raise_()
    _pump()
    return _splash


def step(text: str, fraction: Optional[float] = None) -> None:
    """What start-up is doing now (and how far along, 0..1)."""
    s = _splash
    if s is None:
        return
    s.status.setText(text)
    if fraction is not None:
        s.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
    _pump()


def end() -> None:
    global _splash
    s, _splash = _splash, None
    if s is not None:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(s._watch)
        s.close()
        s.deleteLater()


def current() -> Optional[Splash]:
    return _splash
