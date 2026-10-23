# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""XDF MATH equation handling.

The A2L-derived XDFs carry per-element conversion equations such as
``X``, ``(X)/(8192)``, ``(X)*(10)`` or ``(X)/(10)+(-273.14)``. All observed
equations are linear in X, so they are characterized by probing: raw->display
is ``display = factor * raw + offset``.
"""

import re

_ALLOWED = re.compile(r"^[0-9X\s\.\+\-\*\/\(\)Ee]+$")
_N = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
#: ``(p*X + q) / (r + s*X)`` as the A2L reader writes a rational function
#: (``(f*X-c)/(b-e*X)``): its coefficients are read, not fitted
_RATIONAL = re.compile(r"^\(\s*%s\s*\*\s*X\s*([-+])\s*%s\s*\)\s*/\s*"
                       r"\(\s*%s\s*([-+])\s*%s\s*\*\s*X\s*\)$" % (_N, _N, _N, _N))


class Equation:
    """A raw<->display conversion parsed from an XDF MATH string.

    Linear equations are characterized by probing (``display = factor * raw
    + offset``). A rational one, ``(a*X + b) / (c*X + d)`` -- a frequency
    stored as a period (``7812.5/(X+1)``), a DAMOS reciprocal conversion --
    is fitted the same way and inverted exactly; anything else displays but
    cannot be typed into.
    """

    __slots__ = ("text", "factor", "offset", "linear", "_mobius")

    def __init__(self, text: str = "X"):
        self.text = (text or "X").strip()
        if not _ALLOWED.match(self.text):
            raise ValueError("unsupported equation: %r" % self.text)
        self._mobius = None
        f0, f1, f2 = (self._safe(x) for x in (0.0, 1.0, 2.0))
        if None not in (f0, f1, f2):
            self.offset = f0
            self.factor = f1 - f0
            # linear check: f(2) == f0 + 2*(f1-f0)
            self.linear = abs(f2 - (f0 + 2 * self.factor)) <= 1e-9 * max(
                1.0, abs(f2))
        else:
            self.offset, self.factor, self.linear = 0.0, 0.0, False
        if not self.linear:
            self._mobius = self._exact_mobius() or self._fit_mobius()
            if f1 is not None and f2 is not None:
                self.factor = f2 - f1
                self.offset = f1 - self.factor

    def _eval(self, x: float) -> float:
        return float(eval(self.text, {"__builtins__": {}}, {"X": x}))  # noqa: S307

    def _safe(self, x: float):
        try:
            v = self._eval(x)
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
        return v if v == v and abs(v) != float("inf") else None

    def _exact_mobius(self):
        """``(a, b, d)`` read from the equation's own coefficients when it is
        written ``(p*X+q)/(r+s*X)``. A fit is good to about 1e-13 of its
        scale, which for a reciprocal conversion of a 32-bit value is
        hundreds of counts; the coefficients are exact."""
        m = _RATIONAL.match(self.text.replace(" ", ""))
        if not m:
            return None
        p, sq, q, r, ss, s = m.groups()
        p, q, r, s = float(p), float(q), float(r), float(s)
        q = -q if sq == "-" else q
        s = -s if ss == "-" else s
        if s == 0:
            return None
        a, b, d = p / s, q / s, r / s
        for x in (1.0, 7.0, 1000.0):
            y = self._safe(x)
            if y is None or x + d == 0 or \
                    abs((a * x + b) / (x + d) - y) > 1e-9 * max(1e-300, abs(y)):
                return None
        return (a, b, d)

    def _fit_mobius(self):
        """``(a, b, d)`` with display = (a*X + b) / (X + d), if the equation
        is that (checked at further points), else None."""
        pts = []
        for x in (1.0, 2.0, 3.0, 5.0, 7.0, 11.0, 13.0, 100.0):
            y = self._safe(x)
            if y is not None:
                pts.append((x, y))
        if len(pts) < 5:
            return None
        # a*x + b - d*y = x*y, from the first three points
        rows = [(x, 1.0, -y, x * y) for x, y in pts[:3]]

        def det(m):
            return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                    - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                    + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
        a3 = [r[:3] for r in rows]
        dd = det(a3)
        if abs(dd) < 1e-300:
            return None
        sol = []
        for k in range(3):
            m = [list(r[:3]) for r in rows]
            for i, r in enumerate(rows):
                m[i][k] = r[3]
            sol.append(det(m) / dd)
        a, b, d = sol
        for x, y in pts[3:]:
            den = x + d
            if den == 0 or abs((a * x + b) / den - y) > 1e-7 * max(1.0, abs(y)):
                return None
        return (a, b, d)

    def decode(self, raw) -> float:
        """Raw integer -> display value."""
        if self.linear:
            return self.factor * raw + self.offset
        v = self._safe(float(raw))
        return float("nan") if v is None else v

    def encode(self, display) -> float:
        """Display value -> raw (unrounded; caller clamps/rounds)."""
        if self.linear and self.factor != 0:
            return (display - self.offset) / self.factor
        if self._mobius is not None:
            a, b, d = self._mobius
            if display == a:
                raise ValueError("%s has no stored value" % display)
            return (b - d * display) / (display - a)
        raise ValueError("cannot invert equation: %r" % self.text)

    def __repr__(self):
        return "Equation(%r)" % self.text
