# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for Tools > Options, the toolbar (data/help/options.md) and a
project changed on disk by another program (data/help/projects.md)."""
import os
import tempfile

from help_screenshots import Skip, shot


def _options(ctx, page):
    from ovtsuite.gui_options import OptionsDialog
    w = ctx.main_window(ctx.ref("edc16_lq"))
    return w, ctx.keep(OptionsDialog(w, page))


@shot("main-toolbar", "data/help", size=(1280, 200),
      what="The toolbar, as it comes",
      crop=lambda w: __import__("ovtsuite.gui_toolbar",
                                fromlist=["toolbar"]).toolbar(w))
def toolbar(ctx):
    return ctx.main_window(ctx.ref("edc16_lq"))


@shot("options-general", "data/help", size=(860, 560),
      what="Tools > Options... > General")
def options_general(ctx):
    return _options(ctx, "General")[1]


@shot("options-toolbar", "data/help", size=(860, 600),
      what="Tools > Options... > Toolbar: the button editor")
def options_toolbar(ctx):
    from PySide6 import QtCore
    _w, dlg = _options(ctx, "Toolbar")
    for i in range(dlg.tb_avail.topLevelItemCount()):        # Edit expanded,
        top = dlg.tb_avail.topLevelItem(i)                   # the rest folded
        top.setExpanded(top.text(0) == "Edit")
        for j in range(top.childCount()):
            if top.child(j).data(0, QtCore.Qt.ItemDataRole.UserRole) == \
                    "edit/scale-selection":
                dlg.tb_avail.setCurrentItem(top.child(j))
    dlg.tb_list.setCurrentRow(5)
    return dlg


@shot("options-shortcuts", "data/help", size=(860, 600),
      what="Tools > Options... > Keyboard shortcuts, one changed")
def options_shortcuts(ctx):
    from PySide6 import QtCore
    _w, dlg = _options(ctx, "Keyboard shortcuts")
    want = "file/save-as"
    for i in range(dlg.sc_tree.topLevelItemCount()):
        top = dlg.sc_tree.topLevelItem(i)
        for j in range(top.childCount()):
            it = top.child(j)
            if it.data(0, QtCore.Qt.ItemDataRole.UserRole) == want:
                dlg.sc_tree.setCurrentItem(it)
                dlg._sc_set("Ctrl+Shift+S")     # in the dialog only, not saved
                dlg.sc_tree.scrollToItem(it)
                return dlg
    raise Skip("no %s item" % want)


@shot("project-disk-changed", "data/help", size=(620, 200),
      what="Saving a project another program changed")
def disk_changed(ctx):
    from ovtsuite.gui_disksync import DiskChangedDialog
    w = ctx.main_window(ctx.ref("edc16_lq"))
    box, _answers = DiskChangedDialog.build(w, w.current.project_ref.doc, False)
    return ctx.keep(box)


@shot("project-merge-conflicts", "data/help", size=(820, 300),
      what="Merge changes: what both sides changed differently")
def merge_conflicts(ctx):
    """Real conflicts: the same project changed here and "elsewhere"."""
    from ovtsuite import project as ovt
    from ovtsuite.gui_disksync import MergeConflictDialog
    from ovtsuite.project import disksync
    path = ctx.project(ctx.ref("edc16_lq"), branch="Stage 1")
    d = tempfile.mkdtemp(prefix="ovt-help-merge-")
    copy = os.path.join(d, os.path.basename(path))
    with open(path, "rb") as src, open(copy, "wb") as dst:
        dst.write(src.read())
    mine = ovt.Project.load(copy)
    s = mine.set("ECU")
    head = s.branch("Stage 1").head
    theirs = ovt.Project.load(copy)
    mine.set_info({"vin": "WAUZZZ4F26N012345", "odometer": "182 000 km"})
    mine.set_checkpoint_label(s.id, head, "Read, before the swap")
    theirs.set_info({"vin": "WAUZZZ4F26N012354", "odometer": "182 400 km"})
    theirs.set_checkpoint_label(s.id, head, "As read (AI notes)")
    theirs.save(copy)
    res = disksync.merge(mine, ovt.Project.load(copy), default="mine")
    if not res.conflicts:
        raise Skip("no conflicts")
    return ctx.keep(MergeConflictDialog(res.conflicts, mine.name))


@shot("project-panel", "data/help", size=(1100, 360),
      what="The Project panel, expanded, beside a project tab",
      crop=lambda w: __import__("ovtsuite.gui_project",
                                fromlist=["panel"]).panel(w))
def project_panel(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"), branch="Stage 1", panel=True)
    from ovtsuite import gui_project
    dock = gui_project.panel(w)
    dock.expand()
    dock.refresh()
    dock.tree.expandAll()
    w.resizeDocks([dock], [420], ctx.QtCore.Qt.Orientation.Horizontal)
    return w