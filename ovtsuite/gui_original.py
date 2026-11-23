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


def parts_for(project: Project, sid: str, path: str, data: Optional[bytes] = None
              ) -> Tuple[Images, dict, list, Images]:
    """``(images, source, originals, companions)`` of the file at ``path`` as a
    new original of set ``sid`` (:func:`ovtsuite.project.importers.
    original_parts`); ``companions`` is the EEPROM / MPC the file carries
    beside the flash, which the original does not hold."""
    from .project import importers as _imp
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    try:
        return _imp.original_parts(project, sid, data, os.path.basename(path))
    except _imp.ImportRefused as ex:
        raise ValueError(str(ex))


def images_for(project: Project, sid: str, path: str, data: Optional[bytes] = None
               ) -> Tuple[Images, dict, list]:
    """:func:`parts_for` without the companions."""
    return parts_for(project, sid, path, data)[:3]


def offer_companions(win, doc, sid: str, name: str, companions: Images) -> list:
    """Ask, for each EEPROM / MPC image the new original's file carried, whether
    to put it into the set's linked companion (a new version of it) or, with
    none linked, a companion set of its own. Returns the roles done."""
    from .project import companions as _co
    proj = doc.project
    s = proj.set(sid)
    SB = QtWidgets.QMessageBox.StandardButton
    done = []
    for role, data in companions.items():
        label = "MPC (internal flash)" if role == "mpc" else "EEPROM"
        linked = _co.links(proj, sid).get(role)
        if linked is not None:
            old = _co.head_bytes(proj, linked)
            if old is not None and bytes(old) == bytes(data):
                continue                       # already holds exactly this
            text = ("Add it as a new version of %s, the %s linked to %s?"
                    % (linked.name, label, s.name))
        else:
            text = ("Add it as a %s set linked to %s? %s has none yet." % (
                label, s.name, s.name))
        if QtWidgets.QMessageBox.question(
                win, "Import a new original",
                "%s also holds the %s (%d bytes), which is not part of the "
                "original.\n\n%s" % (name, label, len(data), text),
                SB.Yes | SB.No, SB.Yes) != SB.Yes:
            continue
        try:
            _co.take(proj, sid, role, data, label="Imported %s: %s" % (role.upper(), name))
        except ValueError as ex:
            QtWidgets.QMessageBox.warning(win, "Import a new original", str(ex))
            continue
        done.append(role)
    return done


def differing_bytes(project: Project, sid: str, images: Images,
                    embed=None) -> Tuple[int, int]:
    """``(bytes that differ, bytes compared)`` between ``images`` and the
    set's current original -- with ``embed`` (``{member: offset}``), the
    original where it sits in the larger image."""
    s = project.set(sid)
    old = s.root_of(s.active.head)
    cur = project.state(sid, old.id)
    new = {k: v for k, v in images.items() if k in cur}
    if embed:
        for mid, at in embed.items():
            new[mid] = bytes(images[mid])[at:at + len(cur[mid])]
    recs = _d.diff(new, cur)
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
        images, source, originals, companions = parts_for(proj, sid, path)
        embed = source.get("embed_previous") or None
        why = proj.original_problem(sid, images, embed=embed)
        if why:
            raise ValueError(why)
        if embed and any(t.is_dirty() for t in doc.tabs(win)
                         if t.project_ref.sid == sid):
            raise ValueError(
                "%s has unsaved changes in an open tab. Save or discard them "
                "first: the file is a larger image, and every version of the "
                "binary moves into it." % s.name)
        n, total = differing_bytes(proj, sid, images, embed)
    except (ValueError, OSError) as ex:
        QtWidgets.QMessageBox.warning(win, "Import a new original", str(ex))
        return False
    old = s.root_of(s.active.head)
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, "Import a new original",
        "Make %s the original of %s?" % (name, s.name), SB.Cancel, win)
    where = ("" if not embed else
             " It is a larger image: the current original, and every version "
             "after it, sits in its calibration area (the rest of each is "
             "this file's).")
    box.setInformativeText(
        "The current original (%s) becomes a version based on it, and every "
        "version and branch after it stays as it is.%s Comparing with the "
        "original then compares with %s.\n\n"
        "%d of %d bytes differ between the two (%.1f %%). Save the project "
        "to keep the change." % (
            (old.label or old.kind), where, name, n, total,
            100.0 * n / max(1, total)))
    go = box.addButton("Make it the original",
                       QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(SB.Cancel)
    box.exec()
    if box.clickedButton() is not go:
        return False
    try:
        c = proj.rebase_original(
            sid, images, label="Imported original: %s" % name, source=source,
            embed=embed)
        for fn, fmt, raw, members in originals:
            proj.add_original(sid, fn, fmt, raw, checkpoint=c.id, members=members)
    except ValueError as ex:
        QtWidgets.QMessageBox.warning(win, "Import a new original", str(ex))
        return False
    offer_companions(win, doc, sid, name, companions)
    _gp.reload_set(win, doc, sid)
    _gp.refresh_panel(win)
    try:
        win.statusBar().showMessage(
            "%s is now the original of %s; the one it had is a version "
            "based on it." % (name, s.name), 10000)
    except Exception:                                   # noqa: BLE001
        pass
    return True


def version_images(project: Project, sid: str, after: str, path: str,
                   data: Optional[bytes] = None) -> Tuple[Images, dict]:
    """``(images, source)`` of the file at ``path`` as a version following
    checkpoint ``after``: read as an import would, an EDC16
    calibration-only file laid into ``after``'s full image (not the
    original's), and every part the same size as ``after`` has it.
    ValueError says why the file will not do."""
    from .platforms import edc16 as _e16
    images, source, _originals = images_for(project, sid, path, data)
    if source.get("embed_previous"):
        raise ValueError("The file is a larger image than this binary's "
                         "versions. Import it as the original instead.")
    base = project.state(sid, after)
    for mid, b in list(images.items()):
        if mid not in base:
            raise ValueError("The file holds %s, which this binary does not "
                             "have." % mid)
        if source.get("merged") and len(base[mid]) == _e16.FULL_SIZE \
                and len(b) == _e16.FULL_SIZE:
            # its calibration window, over the boot and program of `after`
            images[mid] = bytes(base[mid][:_e16.CAL_START]) + b[_e16.CAL_START:]
        if len(images[mid]) != len(base[mid]):
            raise ValueError(
                "The file's %s is %d bytes; this version's is %d. A file of "
                "another size is usually another software, not another "
                "version of this one." % (mid, len(images[mid]), len(base[mid])))
    return images, source


def insert_version(win, doc, sid: str, bid: str, cid: str, *,
                   before: bool = False, path: Optional[str] = None) -> bool:
    """Ask for a file and put it in the history of branch ``bid`` as a new
    version right after checkpoint ``cid`` (``before``: right before it --
    before the original, that is the new original, :func:`import_original`).
    The versions after it keep their bytes (:meth:`Project.insert_version`).
    True when it was done."""
    from . import gui_project as _gp
    from .recent import get_last_dir, set_last_dir
    proj = doc.project
    s = proj.set(sid)
    c = s.checkpoint(cid)
    if c is None:
        return False
    if before:
        if c.parent is None:
            return import_original(win, doc, sid, path)
        after = c.parent
    else:
        after = cid
    title = "Insert a version"
    b = s.branch(bid)
    tab = next((t for t in doc.tabs(win) if t.project_ref.sid == sid
                and t.project_ref.bid == bid), None)
    moves_head = b is not None and b.head == after
    if moves_head and tab is not None and tab.is_dirty():
        QtWidgets.QMessageBox.information(
            win, title, "That branch's tab has unsaved changes, and the new "
            "version would become its latest. Save or undo them first.")
        return False
    if not path:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            win, "A version of %s" % s.name, get_last_dir(), _gp._FILES_FILTER)
        if not path:
            return False
        set_last_dir(path)
    name = os.path.basename(path)
    prev = s.checkpoint(after)
    try:
        images, source = version_images(proj, sid, after, path)
        base = proj.state(sid, after)
        n = _d.changed_bytes(_d.diff(base, dict(base, **images)))
        if not n:
            raise ValueError("%s is the same as \"%s\" already."
                             % (name, prev.label or prev.kind))
    except (ValueError, OSError) as ex:
        QtWidgets.QMessageBox.warning(win, title, str(ex))
        return False
    nxt = next((k for k in s.children(after)
                if k.id in {x.id for x in s.lineage(b.head)}), None) \
        if b is not None else None
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, title,
        "Insert %s as a version after \"%s\"?" % (name, prev.label or prev.kind),
        SB.Cancel, win)
    if nxt is not None:
        rest = ("\"%s\" then follows it, with the same bytes as now; every "
                "later version and branch stays as it is."
                % (nxt.label or nxt.kind))
    else:
        rest = "It becomes the latest version of %s." % (b.name if b else "the branch")
    box.setInformativeText(
        "%s\n\n%d bytes differ from \"%s\". Save the project to keep the "
        "change." % (rest, n, prev.label or prev.kind))
    go = box.addButton("Insert", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(SB.Cancel)
    box.exec()
    if box.clickedButton() is not go:
        return False
    try:
        proj.insert_version(sid, after, images, branch=bid,
                            label="Imported: %s" % name, source=source)
    except Exception as ex:                             # noqa: BLE001
        QtWidgets.QMessageBox.warning(win, title, str(ex))
        return False
    if moves_head and tab is not None:
        _gp.reload_tab(tab)
    for t in doc.tabs(win):
        win._workspace_changed(t)
    _gp.refresh_panel(win)
    try:
        win.statusBar().showMessage(
            "%s is a version of %s after \"%s\"." % (
                name, s.name, prev.label or prev.kind), 10000)
    except Exception:                                   # noqa: BLE001
        pass
    return True
