#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build the .ovt project file icon from its SVG sources:

* ``packaging/ovt_project.ico`` -- Windows (the installer points the .ovt
  association's DefaultIcon at it), 16-256 px;
* ``packaging/ovt_project/<n>.png`` -- the Linux package's hicolor
  ``mimetypes/application-x-ovt-project.png`` icons (build_linux.sh).

``ovtsuite/data/ovt_project.svg`` is the icon; ``ovt_project_small.svg`` is
drawn for 16-32 px, where the lettering cannot be read. Rendered with Qt's
SVG renderer (offscreen), packed with Pillow.

    python tools/make_file_icons.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "ovtsuite", "data")
OUT_ICO = os.path.join(ROOT, "packaging", "ovt_project.ico")
OUT_PNG = os.path.join(ROOT, "packaging", "ovt_project")
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(svg, size):
    from PySide6 import QtCore, QtGui, QtSvg
    img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    QtSvg.QSvgRenderer(svg).render(p, QtCore.QRectF(0, 0, size, size))
    p.end()
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    import io
    from PIL import Image
    big = os.path.join(DATA, "ovt_project.svg")
    small = os.path.join(DATA, "ovt_project_small.svg")
    os.makedirs(OUT_PNG, exist_ok=True)
    images = {}
    for n in SIZES:
        png = render(small if n <= 32 else big, n)
        with open(os.path.join(OUT_PNG, "%d.png" % n), "wb") as fh:
            fh.write(png)
        images[n] = Image.open(io.BytesIO(png)).convert("RGBA")
    images[256].save(OUT_ICO, format="ICO", sizes=[(n, n) for n in SIZES],
                     append_images=[images[n] for n in SIZES if n != 256])
    print("%s (%s px)\n%s/*.png" % (OUT_ICO, ", ".join(map(str, SIZES)), OUT_PNG))
    return 0


if __name__ == "__main__":
    sys.exit(main())
