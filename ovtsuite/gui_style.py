# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The application style, adjusted where Qt's platform style ignores a
system setting.

Scroll bars on Windows 11: Qt's ``windows11`` style draws a scroll bar as a
2-pixel line with no track and no arrows until the mouse is over it -- the
Windows 11 "overlay" look -- whatever the user chose. Windows has a setting
for exactly this, *Settings > Accessibility > Visual effects > Always show
scrollbars*, kept as ``HKCU\\Control Panel\\Accessibility\\DynamicScrollbars``
(0 = always show). Qt does not read it, so with it on, the app's scroll bars
were still hidden. :func:`install` puts a thin proxy over the style that, when
the setting asks for it, draws every scroll bar the way the style draws a
hovered one (full width, arrows) on a faint track, as Windows itself does.

Everywhere else this changes nothing: other platforms' styles already follow
their own setting (macOS), or never hide scroll bars (Fusion, the older
Windows styles).
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

#: ``always`` / ``auto`` overrides the system setting (testing, support)
ENV = "OVT_SCROLLBARS"
#: styles that hide their scroll bars until hovered
_OVERLAY_STYLES = ("windows11",)


def windows_always_shows_scrollbars() -> bool:
    """Windows' *Always show scrollbars* setting (False where it cannot be
    read, which is also the Windows default)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Accessibility") as key:
            value, _kind = winreg.QueryValueEx(key, "DynamicScrollbars")
        return int(value) == 0
    except (OSError, ValueError, TypeError):
        return False


def wants_visible_scrollbars() -> bool:
    forced = os.environ.get(ENV, "").strip().lower()
    if forced in ("always", "1", "on"):
        return True
    if forced in ("auto", "0", "off"):
        return False
    return _option_always() or windows_always_shows_scrollbars()


def _option_always() -> bool:
    """Options > Appearance > Scroll bars: Always show."""
    try:
        from . import options
        return options.get("ui/scrollbars") == "always"
    except Exception:
        return False


class VisibleScrollBarStyle(QtWidgets.QProxyStyle):
    """The base style with its scroll bars always drawn as when hovered."""

    def drawComplexControl(self, control, option, painter, widget=None):
        if control == QtWidgets.QStyle.ComplexControl.CC_ScrollBar \
                and isinstance(option, QtWidgets.QStyleOptionSlider):
            opt = QtWidgets.QStyleOptionSlider(option)
            opt.state |= QtWidgets.QStyle.StateFlag.State_MouseOver
            self._track(opt, painter)
            return super().drawComplexControl(control, opt, painter, widget)
        return super().drawComplexControl(control, option, painter, widget)

    @staticmethod
    def _track(opt, painter):
        """A faint track under the thumb, so the bar reads as a scroll bar
        even where the content behind it is plain."""
        color = QtGui.QColor(opt.palette.color(QtGui.QPalette.ColorRole.Text))
        color.setAlpha(18)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        r = QtCore.QRectF(opt.rect).adjusted(1, 1, -1, -1)
        painter.drawRoundedRect(r, 4, 4)
        painter.restore()


def install(app: QtWidgets.QApplication) -> bool:
    """Adjust ``app``'s style where needed. True when it was changed. Call it
    once, before the first window is shown."""
    style = app.style()
    if style is None or isinstance(style, VisibleScrollBarStyle):
        return False
    name = (style.name() or "").lower()
    forced = os.environ.get(ENV, "").strip().lower() in ("always", "1", "on") \
        or _option_always()
    if name not in _OVERLAY_STYLES and not forced:
        return False
    if not wants_visible_scrollbars():
        return False
    base = QtWidgets.QStyleFactory.create(style.name())
    if base is None:
        return False
    app.setStyle(VisibleScrollBarStyle(base))
    return True
