# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Import a new original for a project's binary.

Right-click a binary in the Project panel > **Import a new original...**.
The file (a plain binary, or anything the importers read: ``.sgo``,
``.frf``, ``.dim``, a native container, a WinOLS project's original) becomes
the binary's original -- the root of its history, what "compare with the
original" shows -- and the original it had becomes a version based on it:
the history above it stays exactly as it was
(:meth:`ovtsuite.project.Project.rebase_original`).

For when the first file imported was not the stock one -- a read of an
already tuned car -- and the stock file turns up later. The new original has
to be the same software: the same parts, the same sizes.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6 import QtWidgets

from .project import delta as _d
from .project.project import Project

Images = dict


def images_for(project: Project, sid: str, path: str, data: Optional[bytes] = None
               ) -> Tuple[Images, dict, list]:
    """``(images, source, originals)`` of the file at ``path`` read the way
    importing it into a project would, with its parts named as set ``sid``
    names its own: ``originals`` are the container files the import kept
    byte for byte (``(filename, format, bytes, member ids)``). ValueError
    says why a file will not do."""
    from .project import importers as _imp
    from .project.sessionbridge import _main_member
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    scratch = Project("scratch")
    try:
        s2 = _imp.import_bytes(scratch, data, os.path.basename(path))
    except _imp.ImportRefused as ex:
        raise ValueError(str(ex))
    roots = s2.roots()
    if not roots:
        raise ValueError("%s holds no binary." % os.path.basename(path))
    images = dict(scratch.state(s2.id, roots[0].id))
    here = project.set(sid)
    old = here.root_of(here.active.head) if here.active is not None else None
    if old is not None and set(images) != set(old.sizes):
        # one main image either way (a module names it "flash", the base app
        # "raw"): the same part under the other's name
        mine, theirs = _main_member(project, sid), _main_member(scratch, s2.id)
        if mine is not None and theirs is not None and theirs.id in images \
                and mine.id in old.sizes and mine.id != theirs.id:
            images[mine.id] = images.pop(theirs.id)
    originals = [(o.filename, o.format, scratch.original_bytes(s2.id, o.id),
                  list(o.members)) for o in s2.originals]
    source = dict(roots[0].source or {})
    source.setdefault("file", os.path.basename(path))
    return images, source, originals


def differing_bytes(project: Project, sid: str, images: Images) -> Tuple[int, int]:
    """``(bytes that differ, bytes compared)`` between ``images`` and the
    set's current original."""
    s = project.set(sid)
    old = s.root_of(s.active.head)
    cur = project.state(sid, old.id)
    recs = _d.diff({k: v for k, v in images.items() if k in cur}, cur)
    total = sum(len(v) for v in cur.values())
    return _d.changed_bytes(recs), total


def import_original(win, doc, sid: str, path: Optional[str] = None) -> bool:
    """Ask for a file, check it fits, ask once, and make it the original of
    set ``sid`` in project ``doc``. True when it was done."""
    from . import gui_project as _gp
    from .recent import get_last_dir, set_last_dir
    proj = doc.project
    s = proj.set(sid)
    if not path:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            win, "The original of %s" % s.name, get_last_dir(), _gp._FILES_FILTER)
        if not path:
            return False
        set_last_dir(path)
    name = os.path.basename(path)
    try:
        images, source, originals = images_for(proj, sid, path)
        why = proj.original_problem(sid, images)
        if why:
            raise ValueError(why)
        n, total = differing_bytes(proj, sid, images)
    except (ValueError, OSError) as ex:
        QtWidgets.QMessageBox.warning(win, "Import a new original", str(ex))
        return False
    old = s.root_of(s.active.head)
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, "Import a new original",
        "Make %s the original of %s?" % (name, s.name), SB.Cancel, win)
    box.setInformativeText(
        "The current original (%s) becomes a version based on it, and every "
        "version and branch after it stays as it is. Comparing with the "
        "original then compares with %s.\n\n"
        "%d of %d bytes differ between the two (%.1f %%). Save the project "
        "to keep the change." % (
            (old.label or old.kind), name, n, total, 100.0 * n / max(1, total)))
    go = box.addButton("Make it the original",
                       QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(SB.Cancel)
    box.exec()
    if box.clickedButton() is not go:
        return False
    try:
        c = proj.rebase_original(
            sid, images, label="Imported original: %s" % name, source=source)
        for fn, fmt, raw, members in originals:
            proj.add_original(sid, fn, fmt, raw, checkpoint=c.id, members=members)
    except ValueError as ex:
        QtWidgets.QMessageBox.warning(win, "Import a new original", str(ex))
        return False
    _gp.reload_set(win, doc, sid)
    _gp.refresh_panel(win)
    try:
        win.statusBar().showMessage(
            "%s is now the original of %s; the one it had is a version "
            "based on it." % (name, s.name), 10000)
    except Exception:                                   # noqa: BLE001
        pass
    return True
