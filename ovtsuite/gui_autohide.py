# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A side panel that can be collapsed to a strip and flies out on hover.

:class:`AutoHideDock` is a ``QDockWidget`` for the left or right edge of the
main window with three ways to be:

* **expanded** -- docked as usual; drag the splitter between it and the
  window to resize it;
* **collapsed** -- reduced to a narrow strip with the panel's name written
  along it, so the window gets the space back;
* **flown out** -- while collapsed, resting the mouse on the strip shows the
  whole panel *over* the window (nothing beside it moves), at the width it
  was last given; its outer edge can be dragged to resize it. It goes away
  again when the mouse leaves it (not while one of its menus or dialogs is
  open). Opened from the keyboard or the menu instead, it stays until the
  focus moves elsewhere or Esc is pressed.

The title bar's button collapses; the strip's button (or a click anywhere on
the strip) expands again. Whether the panel is collapsed and how wide it is
are remembered.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .appsettings import settings as _settings

_Area = QtCore.Qt.DockWidgetArea
#: how long the mouse rests on the strip before the panel flies out (ms)
HOVER_DELAY = 250
#: how long it may be away from strip and panel before the panel goes (ms)
LEAVE_DELAY = 350
STRIP_WIDTH = 24


def _truthy(v) -> bool:
    return v not in (False, None, "false", "0", 0, "")


class _Strip(QtWidgets.QWidget):
    """The collapsed panel: its name along a narrow strip, and an expand
    button. Hovering it asks for the flyout, clicking it expands."""

    def __init__(self, dock: "AutoHideDock"):
        super().__init__()
        self.dock = dock
        self.setFixedWidth(STRIP_WIDTH)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip("%s (collapsed) -- rest the mouse here to see it, "
                        "click to expand" % dock.title())
        self.button = QtWidgets.QToolButton(self)
        self.button.setAutoRaise(True)
        self.button.setToolTip("Expand the %s panel" % dock.title())
        self.button.clicked.connect(dock.expand)
        self.button.setFixedSize(STRIP_WIDTH - 2, STRIP_WIDTH - 2)
        self.button.move(1, 1)
        self._timer = QtCore.QTimer(self, singleShot=True,
                                    interval=HOVER_DELAY)
        self._timer.timeout.connect(lambda: dock.fly_out(sticky=False))
        self.sync_arrow()

    def sync_arrow(self):
        left = self.dock.area() != _Area.RightDockWidgetArea
        self.button.setArrowType(QtCore.Qt.ArrowType.RightArrow if left
                                 else QtCore.Qt.ArrowType.LeftArrow)

    def enterEvent(self, ev):
        self._timer.start()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._timer.stop()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self._timer.stop()
            self.dock.expand()
            return
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.ColorRole.Window))
        p.setPen(pal.color(QtGui.QPalette.ColorRole.Mid))
        edge = (self.width() - 1 if self.dock.area() != _Area.RightDockWidgetArea
                else 0)
        p.drawLine(edge, 0, edge, self.height())
        p.setPen(pal.color(QtGui.QPalette.ColorRole.WindowText))
        f = p.font()
        f.setBold(True)
        p.setFont(f)
        # the name reads bottom-to-top on the left, top-to-bottom on the right
        top = STRIP_WIDTH + 6
        if self.dock.area() == _Area.RightDockWidgetArea:
            p.translate(self.width() / 2 + p.fontMetrics().ascent() / 2 - 2, top)
            p.rotate(90)
            p.drawText(0, 0, self.dock.title())
        else:
            w = p.fontMetrics().horizontalAdvance(self.dock.title())
            p.translate(self.width() / 2 + p.fontMetrics().ascent() / 2 - 2,
                        top + w)
            p.rotate(-90)
            p.drawText(0, 0, self.dock.title())
        p.end()


class _Edge(QtWidgets.QWidget):
    """The flyout's outer edge: drag it to resize the panel."""

    def __init__(self, fly: "_Flyout"):
        super().__init__(fly)
        self.fly = fly
        self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Drag to resize")
        self._x0 = self._w0 = None

    def mousePressEvent(self, ev):
        self._x0 = ev.globalPosition().x()
        self._w0 = self.fly.width()

    def mouseMoveEvent(self, ev):
        if self._x0 is None:
            return
        dx = ev.globalPosition().x() - self._x0
        if not self.fly.dock_on_left():
            dx = -dx
        self.fly.set_width(int(self._w0 + dx))

    def mouseReleaseEvent(self, ev):
        self._x0 = None
        self.fly.dock.remember_width(self.fly.width())


class _Flyout(QtWidgets.QFrame):
    """The whole panel shown over the main window while collapsed."""

    EDGE = 5

    def __init__(self, dock: "AutoHideDock"):
        super().__init__(dock.win)
        self.dock = dock
        self.setObjectName("AutoHideFlyout")
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(6, 2, 2, 2)
        self.title = QtWidgets.QLabel("<b>%s</b>" % dock.title())
        head.addWidget(self.title, 1)
        pin = QtWidgets.QToolButton()
        pin.setAutoRaise(True)
        pin.setText("Keep open")
        pin.setToolTip("Expand the panel (dock it beside the window again)")
        pin.clicked.connect(dock.expand)
        head.addWidget(pin)
        lay.addLayout(head)
        self.holder = QtWidgets.QVBoxLayout()
        self.holder.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.holder, 1)
        self.edge = _Edge(self)
        eff = QtWidgets.QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(16)
        eff.setOffset(0, 0)
        self.setGraphicsEffect(eff)
        self.hide()

    def dock_on_left(self) -> bool:
        return self.dock.area() != _Area.RightDockWidgetArea

    def set_width(self, w: int):
        win = self.dock.win
        w = max(self.dock.minimum_body_width(), min(w, win.width() - 80))
        self.place(w)

    def place(self, w=None):
        """Beside the strip, as tall as it, over the window."""
        strip = self.dock.strip
        win = self.dock.win
        w = w or self.width() or self.dock.saved_width()
        tl = strip.mapTo(win, QtCore.QPoint(0, 0))
        h = strip.height()
        if self.dock_on_left():
            x = tl.x() + strip.width()
        else:
            x = tl.x() - w
        self.setGeometry(max(0, x), tl.y(), w, h)
        e = self.EDGE
        self.edge.setGeometry(self.width() - e if self.dock_on_left() else 0,
                              0, e, self.height())
        self.edge.raise_()

    def keyPressEvent(self, ev):
        if ev.key() == QtCore.Qt.Key.Key_Escape:
            self.dock.fly_in()
            return
        super().keyPressEvent(ev)


class AutoHideDock(QtWidgets.QDockWidget):
    """A dock for the left or right edge that collapses to a strip and flies
    out on hover (module doc). Subclasses call :meth:`set_body` with their
    content and give a ``settings_key``."""

    settings_key = "panels/dock"

    def __init__(self, title: str, win):
        super().__init__(title, win)
        self.win = win
        self.setAllowedAreas(_Area.LeftDockWidgetArea | _Area.RightDockWidgetArea)
        self._body = None
        self._collapsed = False
        self._sticky = False
        self._expanded_width = None
        self.strip = _Strip(self)
        self.strip.hide()
        self.flyout = _Flyout(self)
        self._title_bar = self._make_title_bar()
        self._no_title = QtWidgets.QWidget()
        self.setTitleBarWidget(self._title_bar)
        self._watch = QtCore.QTimer(self, interval=120)
        self._watch.timeout.connect(self._check_leave)
        self._away = QtCore.QElapsedTimer()
        self.dockLocationChanged.connect(self._area_changed)
        self.topLevelChanged.connect(self._floating_changed)
        win.installEventFilter(self)
        QtWidgets.QApplication.instance().focusChanged.connect(self._focus_changed)

    def title(self) -> str:
        return self.windowTitle()

    # -- content --------------------------------------------------------
    def set_body(self, widget: QtWidgets.QWidget):
        self._body = widget
        self.setWidget(widget)

    def minimum_body_width(self) -> int:
        return max(200, self._body.minimumSizeHint().width()
                   if self._body is not None else 200)

    # -- title bar ------------------------------------------------------
    def _make_title_bar(self):
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(6, 1, 1, 1)
        lay.setSpacing(0)
        lab = QtWidgets.QLabel("<b>%s</b>" % self.windowTitle())
        lay.addWidget(lab, 1)

        def tool(icon, tip, slot):
            b = QtWidgets.QToolButton()
            b.setAutoRaise(True)
            b.setIcon(self.style().standardIcon(icon))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b
        SP = QtWidgets.QStyle.StandardPixmap
        self._collapse_btn = QtWidgets.QToolButton()
        self._collapse_btn.setAutoRaise(True)
        self._collapse_btn.setToolTip(
            "Collapse to a strip at the edge of the window (rest the mouse on "
            "the strip to see the panel)")
        self._collapse_btn.clicked.connect(self.collapse)
        lay.addWidget(self._collapse_btn)
        self._float_btn = tool(SP.SP_TitleBarNormalButton,
                               "Float the panel in its own window",
                               lambda: self.setFloating(not self.isFloating()))
        tool(SP.SP_TitleBarCloseButton, "Close the panel (View menu opens it "
             "again)", self.close)
        self._sync_collapse_arrow()
        return bar

    def _sync_collapse_arrow(self):
        left = self.area() != _Area.RightDockWidgetArea
        self._collapse_btn.setArrowType(QtCore.Qt.ArrowType.LeftArrow if left
                                        else QtCore.Qt.ArrowType.RightArrow)
        self._collapse_btn.setVisible(not self.isFloating())

    # -- state ----------------------------------------------------------
    def area(self):
        a = self.win.dockWidgetArea(self)
        return a if a != _Area.NoDockWidgetArea else _Area.LeftDockWidgetArea

    def is_collapsed(self) -> bool:
        return self._collapsed

    def saved_width(self) -> int:
        try:
            w = int(_settings().value(self.settings_key + "_width", 0) or 0)
        except (TypeError, ValueError):
            w = 0
        return max(self.minimum_body_width(), w or 300)

    def remember_width(self, w: int):
        _settings().setValue(self.settings_key + "_width", int(w))

    def _remember_collapsed(self):
        _settings().setValue(self.settings_key + "_collapsed", self._collapsed)

    def restore_state(self):
        """Collapsed again if it was last time (call once it is docked)."""
        if _truthy(_settings().value(self.settings_key + "_collapsed", False)):
            self.collapse(remember=False)       # keep the width it had
        else:
            self.win.resizeDocks([self], [self.saved_width()],
                                 QtCore.Qt.Orientation.Horizontal)

    # -- collapse / expand ---------------------------------------------
    def collapse(self, remember: bool = True):
        if self._collapsed or self.isFloating() or self._body is None:
            return
        if remember and self.isVisible() and self.width() > STRIP_WIDTH:
            self.remember_width(self.width())
        self._collapsed = True
        self.setTitleBarWidget(self._no_title)          # no title bar
        self.setWidget(self.strip)
        self._body.hide()
        self._body.setParent(None)
        self.strip.sync_arrow()
        self.strip.show()
        self.setMinimumWidth(STRIP_WIDTH)
        self.win.resizeDocks([self], [STRIP_WIDTH],
                             QtCore.Qt.Orientation.Horizontal)
        self._remember_collapsed()

    def expand(self):
        if not self._collapsed:
            return
        self.fly_in()
        self._collapsed = False
        self.strip.hide()
        self.strip.setParent(None)
        self.setTitleBarWidget(self._title_bar)
        self.setWidget(self._body)
        self._body.show()
        self.setMinimumWidth(self.minimum_body_width())
        self.win.resizeDocks([self], [self.saved_width()],
                             QtCore.Qt.Orientation.Horizontal)
        self._remember_collapsed()
        self._sync_collapse_arrow()

    # -- flyout ---------------------------------------------------------
    def is_flown_out(self) -> bool:
        return self.flyout.isVisible()

    def fly_out(self, sticky: bool = False):
        if not self._collapsed or not self.isVisible():
            return
        self._sticky = sticky or self._sticky
        if not self.flyout.isVisible():
            self._body.setParent(self.flyout)
            self.flyout.holder.addWidget(self._body)
            self._body.show()
            self.flyout.place(self.saved_width())
            self.flyout.show()
            self.flyout.raise_()
        self._away.invalidate()
        self._watch.start()

    def fly_in(self):
        self._watch.stop()
        self._sticky = False
        if self.flyout.isVisible():
            self.remember_width(self.flyout.width())
            self.flyout.hide()
            self.flyout.holder.removeWidget(self._body)
            self._body.setParent(None)
            self._body.hide()

    def _inside(self) -> bool:
        pos = QtGui.QCursor.pos()
        for w in (self.flyout, self.strip):
            if w.isVisible() and w.rect().contains(w.mapFromGlobal(pos)):
                return True
        return False

    def _check_leave(self):
        if not self.flyout.isVisible():
            self._watch.stop()
            return
        app = QtWidgets.QApplication
        busy = (app.activePopupWidget() is not None
                or app.activeModalWidget() is not None
                or app.mouseButtons() != QtCore.Qt.MouseButton.NoButton)
        if self._sticky or busy or self._inside():
            self._away.invalidate()
            return
        if not self._away.isValid():
            self._away.start()
        elif self._away.elapsed() >= LEAVE_DELAY:
            self.fly_in()

    def _focus_changed(self, _old, new):
        if not (self._sticky and self.flyout.isVisible()):
            return
        w = new
        while w is not None:
            if w is self.flyout or w is self.strip:
                return
            w = w.parentWidget()
        if new is not None:
            self.fly_in()

    def show_and_focus(self, focus_widget=None):
        """Show the panel; when collapsed, fly it out until the focus leaves
        it (opened from the menu or the keyboard)."""
        self.show()
        if self._collapsed:
            self.fly_out(sticky=True)
        else:
            self.raise_()
        if focus_widget is not None:
            focus_widget.setFocus()

    # -- reacting -------------------------------------------------------
    def _area_changed(self, _area):
        self.strip.sync_arrow()
        self.strip.update()
        self._sync_collapse_arrow()
        if self.flyout.isVisible():
            self.flyout.place()

    def _floating_changed(self, floating):
        if floating and self._collapsed:
            self.expand()
        self._sync_collapse_arrow()

    def hideEvent(self, ev):
        self.fly_in()
        super().hideEvent(ev)

    def eventFilter(self, obj, ev):
        if obj is self.win and ev.type() in (QtCore.QEvent.Type.Resize,
                                             QtCore.QEvent.Type.Move) \
                and self.flyout.isVisible():
            self.flyout.place()
        return False


__all__ = ["AutoHideDock", "HOVER_DELAY", "LEAVE_DELAY", "STRIP_WIDTH"]
