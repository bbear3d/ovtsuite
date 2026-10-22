# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The dyno chart's exports (:mod:`ovtsuite.gui_dyno`): a CSV of every run
with the conditions it was worked out at, and the chart as an image with a
caption -- the files, the peaks, the conditions -- so it reads on its own.
"""

from __future__ import annotations

import csv
import io
import time
from typing import List

from PySide6 import QtCore, QtGui

from . import branding
from . import dyno as _dy


def conditions_lines(state) -> List[str]:
    """The conditions, the reference and the engine, one line each."""
    c = state.cond
    m = state.model
    d, cyl = m.engine(c) if m is not None else (c.displacement or 0, c.cylinders or 0)
    out = ["Air pressure %.0f hPa, charge %.0f °C, coolant %.0f °C, "
           "volumetric efficiency %.2f, drivetrain loss %.0f %%" % (
               c.baro, c.charge_temp, c.coolant, c.ve, c.loss)]
    extra = []
    if c.boost_cap:
        extra.append("boost limited to %.0f hPa" % c.boost_cap)
    if getattr(c, "overboost", False):
        extra.append("overboost applied")
    if m is not None:
        wish = dict(m.wish_choices).get(c.wish, "")
        if wish:
            extra.append("driver's wish: %s" % wish)
        smoke = [lbl for k, lbl, _d in m.smoke_choices if k in m.smoke_on(c)]
        if smoke:
            extra.append("smoke limit: %s" % ", ".join(smoke))
        if m.group:
            extra.append("dataset %s" % m.group)
    if extra:
        out.append("; ".join(extra))
    out.append("Engine %s l, %s cylinders" % ("%g" % d if d else "?", cyl or "?"))
    out.append("Reference (its conversion for every run): %s" % (state.ref_label or "none"))
    if state.curve is not None and state.curve.torque_source:
        out.append("Torque from: %s" % state.curve.torque_source)
    k = state.speed_factor()
    if k:
        out.append("Road speed in gear %d: %.1f km/h per 1000 rpm"
                   % (state.xaxis.get("gear") or 0, k * 1000))
    return out


def _runs(state):
    return [r for r in state.runs if r.curve is not None and r.curve.points]


def csv_text(state) -> str:
    """Every run as CSV: ``#`` lines first (the file, the runs, the
    conditions), then one row per engine speed."""
    p = getattr(state.ws, "primary", None)
    pu, tu = state.power_unit, state.torque_unit
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    buf.write("# %s dyno chart, %s\n" % (branding.APP_NAME, time.strftime("%Y-%m-%d %H:%M")))
    if p is not None:
        buf.write("# File: %s\n" % p.binfile.name)
    for line in conditions_lines(state):
        buf.write("# %s\n" % line)
    runs = _runs(state)
    for r in runs:
        buf.write("# Run %s: %s\n" % (r.label, r.detail or r.label))
    buf.write("# Calculated from the calibration at full load, not measured.\n")
    pl, tl = _dy.power_label(pu), _dy.torque_label(tu)
    k = state.speed_factor()
    head = ["rpm"] + (["km/h"] if k else [])
    for r in runs:
        head += ["%s power (%s)" % (r.label, pl), "%s torque (%s)" % (r.label, tl),
                 "%s quantity (mg/stroke)" % r.label, "%s limited by" % r.label]
    w.writerow(head)
    rpms = sorted({pt.rpm for r in runs for pt in r.curve.points})
    idx = [{pt.rpm: pt for pt in r.curve.points} for r in runs]
    for rpm in rpms:
        row = ["%g" % rpm] + (["%.1f" % (rpm * k)] if k else [])
        for r, ix in zip(runs, idx):
            pt = ix.get(rpm)
            if pt is None:
                row += ["", "", "", ""]
                continue
            lim = r.curve.limiters.get(pt.limiter)
            row += ["%.1f" % _dy.power_in(pt.power, pu), "%.1f" % _dy.torque_in(pt.torque, tu),
                    "%.2f" % pt.q, lim.label if lim else pt.limiter]
        w.writerow(row)
    return buf.getvalue()


def image(state, width: int = 1400, height: int = 760, show_power: bool = True,
          show_torque: bool = True) -> QtGui.QImage:
    """The chart at ``width`` x ``height`` with a caption under it: the
    file, each run's peaks, the conditions."""
    from .gui_dyno import DynoChart, run_colour
    chart = DynoChart(state)
    chart.show_power, chart.show_torque = show_power, show_torque
    chart.resize(width, height)
    pm = chart.grab()
    chart.deleteLater()
    pu, tu = state.power_unit, state.torque_unit
    p_ = getattr(state.ws, "primary", None)
    lines = []
    for r in _runs(state):
        pp, pt = r.curve.peak_power(), r.curve.peak_torque()
        lines.append((r.slot, "%s:  %s %s @ %d rpm,  %s %s @ %d rpm" % (
            r.detail if r.key == "ref" and r.detail else r.label,
            "%.1f" % _dy.power_in(pp.power, pu), _dy.power_label(pu), pp.rpm,
            "%.0f" % _dy.torque_in(pt.torque, tu), _dy.torque_label(tu), pt.rpm)))
    cond = conditions_lines(state)
    font = QtGui.QFont(chart.font())
    fm = QtGui.QFontMetrics(font)
    lh = fm.height() + 2
    title = "%s -- %s" % (p_.binfile.name if p_ is not None else "Dyno chart",
                          time.strftime("%Y-%m-%d"))
    cap_h = lh * (2 + len(lines) + len(cond)) + 16
    dpr = pm.devicePixelRatio() or 1.0
    img = QtGui.QImage(int(width * dpr), int((height + cap_h) * dpr),
                       QtGui.QImage.Format_ARGB32)
    img.setDevicePixelRatio(dpr)
    pal = chart.palette()
    img.fill(pal.color(QtGui.QPalette.Base))
    p = QtGui.QPainter(img)
    try:
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.drawPixmap(0, 0, pm)
        ink = pal.color(QtGui.QPalette.WindowText)
        muted = QtGui.QColor(ink)
        muted.setAlpha(160)
        dark = pal.color(QtGui.QPalette.Base).lightness() < 128
        y = height + 8
        bold = QtGui.QFont(font)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(ink)
        p.drawText(QtCore.QPointF(12, y + fm.ascent()), title)
        p.setFont(font)
        y += lh
        for slot, text in lines:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(run_colour(slot, dark))
            p.drawRoundedRect(QtCore.QRectF(12, y + fm.height() / 2 - 4, 12, 8), 2, 2)
            p.setPen(ink)
            p.drawText(QtCore.QPointF(30, y + fm.ascent()),
                       fm.elidedText(text, QtCore.Qt.ElideMiddle, width - 42))
            y += lh
        p.setPen(muted)
        for text in cond + ["Calculated from the calibration at full load, not "
                            "measured (%s)." % branding.APP_NAME]:
            p.drawText(QtCore.QPointF(12, y + fm.ascent()),
                       fm.elidedText(text, QtCore.Qt.ElideRight, width - 24))
            y += lh
    finally:
        p.end()
    return img


__all__ = ["csv_text", "image", "conditions_lines"]
