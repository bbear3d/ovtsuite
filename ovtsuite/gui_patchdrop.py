# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patch files dragged in and out of a project.

*In*: a patch file dropped on the main window is recognised (``.patch``, or
a JSON file with a patch's shape) and offers a menu -- keep it in a project,
apply it to the tab in front, or both -- instead of being read as a
binary. (:func:`split_patches`, :func:`handle_patches`, called from
``gui_files.handle_drop``.)

*Out*: the Project panel's logs, patches and attachments can be dragged to a
folder or another program; they are written to a scratch folder first
(:func:`drag_mime`). A drop of such a file back on the window is ignored
(:func:`is_own_drag`).
"""
import atexit
import json
import os
import shutil
import tempfile
import time
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

#: a file this big is not sniffed for patch content (patches are small JSON)
_SNIFF_MAX = 32 * 1024 * 1024


def is_patch_file(path: str) -> bool:
    """A patch file: ``*.patch``, or a small JSON object with ``entries`` /
    ``blocks`` lists. Anything unreadable is not one."""
    try:
        if not os.path.isfile(path):
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext == ".patch":
            return True
        if ext not in (".json", ".txt", "") or os.path.getsize(path) > _SNIFF_MAX:
            return False
        with open(path, "rb") as fh:
            head = fh.read(64).lstrip()
            if not head.startswith(b"{"):
                return False
            fh.seek(0)
            d = json.loads(fh.read().decode("utf-8"))
        return isinstance(d, dict) and isinstance(d.get("entries", []), list) \
            and isinstance(d.get("blocks", []), list) \
            and ("entries" in d or "blocks" in d)
    except (OSError, ValueError):
        return False


def split_patches(paths: List[str]) -> Tuple[List[str], List[str]]:
    """``(patch files, the others)`` of ``paths``, in order."""
    patches = [p for p in paths if is_patch_file(p)]
    return patches, [p for p in paths if p not in patches]


def read_patch(path: str):
    """The :class:`ovtsuite.patch.Patch` in ``path`` and its raw bytes;
    ValueError (with a plain message) when it is not a readable patch."""
    from . import patch as _patch
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return _patch.Patch.from_dict(json.loads(raw.decode("utf-8"))), raw
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError,
            AttributeError) as ex:
        raise ValueError("%s: %s" % (os.path.basename(path), ex))


def keep_in(win, doc, sid, path: str, raw: bytes):
    """Keep the patch ``raw`` (from file ``path``) in ``doc``, for set
    ``sid``; returns the stored attachment."""
    from . import gui_project as _gp
    att = doc.project.add_patch(os.path.basename(path), raw, set_id=sid)
    for t in doc.tabs(win):
        win._workspace_changed(t)
    _gp.refresh_panel(win)
    _gp.autosave_soon(win, doc)
    return att


def apply_to(ws, pt, name: str) -> None:
    """Apply the patch ``pt`` to tab ``ws`` through the usual dialog."""
    from .gui import PatchApplyDialog
    PatchApplyDialog(ws.primary, pt, name, ws).exec()
    ws.model.refresh()
    ws._status()


def _targets(win):
    """``(label, doc, sid)`` for each open project a patch can be kept in,
    the front tab's first."""
    from . import gui_project as _gp
    cur = win.current
    ref = getattr(cur, "project_ref", None)
    out, seen = [], set()
    if ref is not None:
        out.append((ref.doc.title, ref.doc, ref.sid))
        seen.add(id(ref.doc))
    for d in _gp.docs(win):
        if id(d) not in seen:
            out.append((d.title, d, None))
            seen.add(id(d))
    return out


def _exec_menu(menu, pos):
    """The action picked from ``menu`` (a seam for tests)."""
    return menu.exec(pos)


def handle_patches(win, paths: List[str], global_pos=None) -> bool:
    """Patch files dropped on the window: a menu of keep / apply / both.
    True when something was done."""
    good, bad = [], []
    for p in paths:
        try:
            pt, raw = read_patch(p)
            good.append((p, pt, raw))
        except ValueError as ex:
            bad.append(str(ex))
    if bad:
        QtWidgets.QMessageBox.warning(
            win, "Patch", "Not a readable patch:\n%s" % "\n".join(bad))
    if not good:
        return False
    cur = win.current
    can_apply = cur is not None and cur.primary is not None
    targets = _targets(win)
    if not targets and not can_apply:
        QtWidgets.QMessageBox.information(
            win, "Patch", "Open a project or a binary first: a patch is kept "
            "in a project or applied to the file in front.")
        return False
    names = ", ".join(os.path.basename(p) for p, _pt, _r in good[:3])
    if len(good) > 3:
        names += ", ..."
    menu = QtWidgets.QMenu(win)
    menu.addAction(names).setEnabled(False)
    menu.addSeparator()
    keep, both = {}, {}
    for label, doc, sid in targets[:6]:
        a = menu.addAction("Keep in %s" % label)
        keep[a] = (doc, sid)
        if can_apply and sid is not None:
            b = menu.addAction("Keep in %s and apply to this tab" % label)
            both[b] = (doc, sid)
    only = menu.addAction("Apply to this tab") if can_apply else None
    first = next(iter(keep), None) if keep else only
    if first is not None:
        menu.setDefaultAction(first)
    act = _exec_menu(menu, global_pos or QtGui.QCursor.pos())
    if act is None:
        return False
    if act is only:
        for p, pt, _raw in good:
            apply_to(cur, pt, os.path.basename(p))
        return True
    pick = keep.get(act) or both.get(act)
    if pick is None:
        return False
    doc, sid = pick
    for p, _pt, raw in good:
        keep_in(win, doc, sid, p, raw)
    if act in both:
        for p, pt, _raw in good:
            apply_to(cur, pt, os.path.basename(p))
    return True


# ---------------------------------------------------------------- drag out
_ROOT: Optional[str] = None


def _root() -> str:
    global _ROOT
    if _ROOT is None:
        _ROOT = os.path.join(tempfile.gettempdir(), "ovtsuite-drag-%d" % os.getpid())
        os.makedirs(_ROOT, exist_ok=True)
        atexit.register(shutil.rmtree, _ROOT, True)
    return _ROOT


def is_own_drag(path: str) -> bool:
    """True for a file this app wrote for a drag out of the Project panel."""
    if _ROOT is None:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), _ROOT]) == _ROOT
    except ValueError:
        return False


def _scratch(name: str, raw: bytes) -> str:
    """``raw`` written to a fresh scratch folder under its own file name."""
    d = tempfile.mkdtemp(dir=_root())
    path = os.path.join(d, os.path.basename(name) or "file")
    with open(path, "wb") as fh:
        fh.write(raw)
    # older drags are no longer wanted (the drop has long finished)
    cutoff = time.time() - 3600
    for e in os.listdir(_root()):
        p = os.path.join(_root(), e)
        try:
            if p != d and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, True)
        except OSError:
            pass
    return path


def drag_mime(items) -> Optional[QtCore.QMimeData]:
    """Mime data (file urls) for the logs, patches and attachments among the
    Project panel ``items``; None when there are none. Logs go out as kept."""
    from . import gui_project as _gp
    urls = []
    for it in items:
        data = it.data(0, _gp._ROLE)
        if not data or data[0] not in ("log", "patch", "attachment"):
            continue
        try:
            a, raw = _gp._att(data[1], data[0], data[2])
        except (StopIteration, KeyError):
            continue
        urls.append(QtCore.QUrl.fromLocalFile(_scratch(a.filename, raw)))
    if not urls:
        return None
    mime = QtCore.QMimeData()
    mime.setUrls(urls)
    return mime


class DragTree(QtWidgets.QTreeWidget):
    """The Project panel's tree: its kept files can be dragged out."""

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)

    def mimeData(self, items):
        return drag_mime(items)

    def mimeTypes(self):
        return ["text/uri-list"]

    def supportedDragActions(self):
        return QtCore.Qt.DropAction.CopyAction
