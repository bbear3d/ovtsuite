# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Small pieces that keep a side panel narrow: a check box whose text wraps
instead of setting the panel's width, a combo box that does not size itself
to its longest item (its list still shows every item whole), and a spin box
that stays at its own width."""
from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ["WrapCheckBox", "compact_combo", "fixed_width"]


class WrapCheckBox(QtWidgets.QCheckBox):
    """A check box whose text wraps onto further lines when the width runs
    out. Its minimum width is the box plus the longest word; ``text()``,
    ``setText()`` and the signals are a QCheckBox's own."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Preferred)
        # a check box is one line high and fixed there; this one grows with
        # its wrapped text
        sp.setVerticalPolicy(QtWidgets.QSizePolicy.Minimum)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _option(self) -> QtWidgets.QStyleOptionButton:
        opt = QtWidgets.QStyleOptionButton()
        self.initStyleOption(opt)
        return opt

    def _indent(self) -> int:
        st, opt = self.style(), self._option()
        return (st.pixelMetric(QtWidgets.QStyle.PM_IndicatorWidth, opt, self)
                + st.pixelMetric(QtWidgets.QStyle.PM_CheckBoxLabelSpacing, opt, self) + 2)

    def _text_height(self, width: int) -> int:
        fm = self.fontMetrics()
        r = fm.boundingRect(QtCore.QRect(0, 0, max(1, width - self._indent()), 100000),
                            int(QtCore.Qt.TextWordWrap | QtCore.Qt.AlignLeft), self.text())
        return r.height()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        one = super().sizeHint().height()
        return max(one, self._text_height(width) + (one - self.fontMetrics().height()))

    def minimumSizeHint(self) -> QtCore.QSize:
        fm = self.fontMetrics()
        word = max((fm.horizontalAdvance(w) for w in self.text().split()), default=0)
        return QtCore.QSize(self._indent() + word + 2, super().minimumSizeHint().height())

    def sizeHint(self) -> QtCore.QSize:
        return super().sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _ev):
        p = QtWidgets.QStylePainter(self)
        opt = self._option()
        st = self.style()
        fm = self.fontMetrics()
        ind = QtWidgets.QStyleOptionButton(opt)
        ind.rect = st.subElementRect(QtWidgets.QStyle.SE_CheckBoxIndicator, opt, self)
        # the box beside the first line, not the middle of a wrapped text
        line = super().sizeHint().height()
        ind.rect.moveTop(max(0, (line - ind.rect.height()) // 2))
        p.drawPrimitive(QtWidgets.QStyle.PE_IndicatorCheckBox, ind)
        x = self._indent()
        top = max(0, (line - fm.height()) // 2)
        rect = QtCore.QRect(x, top, max(1, self.width() - x), self.height() - top)
        flags = int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap
                    | QtCore.Qt.TextShowMnemonic)
        st.drawItemText(p, rect, flags, opt.palette, self.isEnabled(), self.text(),
                        QtGui.QPalette.WindowText)
        if opt.state & QtWidgets.QStyle.State_HasFocus:
            fr = QtWidgets.QStyleOptionFocusRect()
            fr.initFrom(self)
            fr.rect = fm.boundingRect(rect, flags, self.text()).adjusted(-1, 0, 1, 1)
            p.drawPrimitive(QtWidgets.QStyle.PE_FrameFocusRect, fr)


def compact_combo(box: QtWidgets.QComboBox, chars: int = 12) -> QtWidgets.QComboBox:
    """Size ``box`` for ``chars`` characters rather than its longest item; a
    layout may still widen it. The drop-down list stays wide enough to show
    every item whole (also items added later)."""
    box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(chars)
    view = box.view()

    def fit(*_):
        w = view.sizeHintForColumn(0)
        if w > 0:
            view.setMinimumWidth(w + 2 * view.frameWidth()
                                 + view.verticalScrollBar().sizeHint().width() + 4)
    box.model().rowsInserted.connect(fit)
    box.model().modelReset.connect(fit)
    fit()
    return box


def fixed_width(*widgets):
    """Keep each widget at its own size hint across: a spin box in a form
    then does not stretch over the whole panel."""
    for w in widgets:
        sp = w.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Fixed)
        w.setSizePolicy(sp)
    return widgets
