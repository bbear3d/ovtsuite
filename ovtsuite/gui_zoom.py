# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Zoom for the table views -- the map table (and its table tabs) and the
hex editor: Ctrl + mouse wheel, Ctrl + = / Ctrl + - and Ctrl + 0 (back to
100 %). The scale is remembered (``zoom/<key>``) and shared by every view of
the same kind: zooming one map table zooms the table of every tab, now and
the next time the app starts.
"""

from __future__ import annotations

import weakref
from typing import Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding

MIN, MAX, STEP = 0.6, 3.0, 1.1

_views: Dict[str, List["weakref.ref"]] = {}


def scale(key: str) -> float:
    s = QtCore.QSettings(*branding.QSETTINGS)
    try:
        v = float(s.value("zoom/%s" % key, 1.0) or 1.0)
    except (TypeError, ValueError):
        v = 1.0
    return max(MIN, min(MAX, v))


def set_scale(key: str, value: float) -> float:
    value = max(MIN, min(MAX, round(float(value), 3)))
    s = QtCore.QSettings(*branding.QSETTINGS)
    s.setValue("zoom/%s" % key, value)
    s.sync()
    for ref in list(_views.get(key, [])):
        z = ref()
        if z is None:
            _views[key].remove(ref)
            continue
        z.apply(value)
    return value


class _Zoom(QtCore.QObject):
    """Holds a view's base font and sizes; scales them."""

    def __init__(self, view: QtWidgets.QAbstractItemView, key: str,
                 after: Optional[Callable[[float], None]] = None):
        super().__init__(view)
        self.view = view
        self.key = key
        self.after = after
        self.base_font = QtGui.QFont(view.font())
        self.base_head = QtGui.QFont(view.horizontalHeader().font()) \
            if hasattr(view, "horizontalHeader") else None
        self.base_row = view.verticalHeader().defaultSectionSize() \
            if hasattr(view, "verticalHeader") else 0
        view.viewport().installEventFilter(self)
        for seq, fn in (("Ctrl+=", self.zoom_in), ("Ctrl++", self.zoom_in),
                        ("Ctrl+-", self.zoom_out), ("Ctrl+0", self.reset)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), view)
            sc.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    # -- the scale -----------------------------------------------------------
    def apply(self, factor: float):
        v = self.view
        f = QtGui.QFont(self.base_font)
        size = self.base_font.pointSizeF()
        if size > 0:
            f.setPointSizeF(max(5.0, size * factor))
        else:
            f.setPixelSize(max(7, int(round(self.base_font.pixelSize() * factor))))
        v.setFont(f)
        if self.base_head is not None:
            h = QtGui.QFont(self.base_head)
            hs = self.base_head.pointSizeF()
            if hs > 0:
                h.setPointSizeF(max(5.0, hs * factor))
            v.horizontalHeader().setFont(h)
            v.verticalHeader().setFont(h)
        if self.base_row:
            v.verticalHeader().setDefaultSectionSize(
                max(12, int(round(self.base_row * factor))))
        if self.after is not None:
            try:
                self.after(factor)
            except Exception:
                pass

    def zoom_in(self):
        set_scale(self.key, scale(self.key) * STEP)

    def zoom_out(self):
        set_scale(self.key, scale(self.key) / STEP)

    def reset(self):
        set_scale(self.key, 1.0)

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Wheel and \
                ev.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            dy = ev.angleDelta().y()
            if dy > 0:
                self.zoom_in()
            elif dy < 0:
                self.zoom_out()
            return True
        return False


def install(view: QtWidgets.QAbstractItemView, key: str,
            after: Optional[Callable[[float], None]] = None) -> _Zoom:
    """Give ``view`` zoom, at the scale remembered for ``key``. ``after``
    is called with the factor once the font is set (to size columns)."""
    z = _Zoom(view, key, after)
    _views.setdefault(key, []).append(weakref.ref(z))
    f = scale(key)
    if f != 1.0:
        z.apply(f)
    return z


__all__ = ["install", "scale", "set_scale"]
