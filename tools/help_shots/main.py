# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Recipes for the main window and file-editing pages (ids ``main-...``):
data/help/interface.md, editing.md, getting-started.md and editor/*.md.

Everything opens the EDC16 reference binary, imported into a project (as the
user does: there are no plain file tabs) saved in a temporary folder -- the
reference file itself is never written. A project tab starts out compared
with the original as read.
"""
import atexit
import os
import shutil
import tempfile

from help_screenshots import shot

try:                                    # the real File information dialog --
    from ovtsuite import gui as _gui    # the tool swaps it out for a no-op
    _SHOW_FILE_INFO = _gui.Workspace.show_file_info
except Exception:                       # pragma: no cover - no Qt
    _SHOW_FILE_INFO = None

#: the map most screenshots show: injection duration, 10 x 10, both axes;
#: the Q file's copy is the same size with different axes
MAP = "InjVlv_phiInjMI1_MAP0"
#: a second table, for a kept tab beside the preview
MAP2 = "EngPrt_trqLimP_MAP"


_TEMP = []


@atexit.register
def _cleanup():
    for d in _TEMP:
        shutil.rmtree(d, ignore_errors=True)


def _tempdir():
    d = tempfile.mkdtemp(prefix="ovt-help-")
    _TEMP.append(d)
    return d


def _ws(w):
    return w.current


def _split(ws, left):
    from PySide6 import QtWidgets
    sp = ws.findChild(QtWidgets.QSplitter)
    if sp is not None:
        sp.setSizes([left, 2000])


def _expand(ws, *names):
    """Expand the tree headings whose text starts with each of ``names``
    (a path: top-level group, then sub-heading)."""
    node = ws.tree.invisibleRootItem()
    for name in names:
        for i in range(node.childCount()):
            ch = node.child(i)
            if ch.text(0).startswith(name):
                ch.setExpanded(True)
                node = ch
                break
    return node


def _select(ws, r0, c0, r1, c1):
    from PySide6 import QtCore
    m = ws.model
    sm = ws.table.selectionModel()
    F = QtCore.QItemSelectionModel.SelectionFlag
    sm.setCurrentIndex(m.index(r0, c0), F.NoUpdate)
    sm.select(QtCore.QItemSelection(m.index(r0, c0), m.index(r1, c1)),
              F.ClearAndSelect)
    _top(ws)


def _top(ws):
    """Scroll the grid back to the top once the window has its final size
    (showing and resizing it scrolls the current cell into view)."""
    from PySide6 import QtCore
    ws.table.scrollToTop()
    for ms in (450, 520):
        QtCore.QTimer.singleShot(ms, ws.table.scrollToTop)


def _edit(ws, name, cells):
    e = next(x for x in ws.primary.entities if x.name == name)
    ws.primary.edit_cells(e, cells)
    return e


def _bump(ws, name, factor=1.08, rows=range(5, 9), cols=range(3, 8)):
    """Raise a block of a table's cells, as a tune would."""
    e = next(x for x in ws.primary.entities if x.name == name)
    _edit(ws, name, [(r, c, e.cell(r, c) * factor) for r in rows for c in cols])
    return e


def _q_pair(ctx):
    """The LQ reference file with the Q file (another software) as its
    comparison."""
    w = ctx.main_window(ctx.ref("edc16_lq"), comparison=ctx.ref("edc16_q"))
    return w, _ws(w)


def _tuned_pair(ctx):
    """The LQ file's project on a *Stage 1* branch with two maps raised
    (unsaved), compared -- as a project tab starts out -- with the original
    as read."""
    w = ctx.main_window(ctx.ref("edc16_lq"), branch="Stage 1")
    ws = _ws(w)
    _bump(ws, MAP)
    _bump(ws, MAP2, 1.1, rows=range(10, 16), cols=range(0, 4))
    return w, ws


def _opened(ctx, cls):
    """The visible dialog of class ``cls`` a menu command just opened (the
    tool makes ``exec`` only show it)."""
    from PySide6 import QtWidgets
    ctx.settle(100)
    dlg = next((d for d in QtWidgets.QApplication.topLevelWidgets()
                if isinstance(d, cls) and d.isVisible()), None)
    if dlg is None:
        from help_screenshots import Skip
        raise Skip("%s did not open" % cls.__name__)
    return ctx.keep(dlg)


# -- the main window ----------------------------------------------------------
@shot("main-overview", "data/help", size=(1280, 800),
      what="The main window: tree, table tabs, a map with a comparison")
def overview(ctx):
    w, ws = _q_pair(ctx)
    _split(ws, 380)
    ctx.open_table(w, MAP2, pin=True)
    ctx.open_table(w, MAP)
    _expand(ws, "Power", "Torque limit")
    _expand(ws, "Power", "Injection duration")
    return w


@shot("main-tree", "data/help", size=(1280, 760),
      what="The parameter tree: groups, Advanced, filter, counts",
      crop=lambda w: w.current.tree.parentWidget())
def tree(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _split(ws, 520)
    for name in ("Power", "Limiters", "Drivability"):
        _expand(ws, name)
    _expand(ws, "Power", "Driver")
    ctx.open_table(w, MAP)
    return w


@shot("main-tree-differ", "data/help", size=(1280, 560),
      what="Show only parameters that differ, with a comparison loaded",
      crop=lambda w: w.current.tree.parentWidget())
def tree_differ(ctx):
    w, ws = _q_pair(ctx)
    _split(ws, 520)
    ws.diff_only.setChecked(True)
    ctx.settle(200)
    _expand(ws, "Power", "Torque limit")
    _expand(ws, "Power", "Injection duration")
    return w


@shot("main-table", "data/help", size=(1300, 600),
      what="A map: table tabs, title and notes, the edit bar, axes, a selection",
      crop=lambda w: w.current.table.parentWidget())
def table(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _split(ws, 300)
    ctx.open_table(w, MAP2, pin=True)
    ctx.open_table(w, MAP)
    _select(ws, 2, 3, 5, 7)
    ws.table.setFocus()
    return w


@shot("main-heatmap", "data/help", size=(1300, 600),
      what="The same map with Heatmap ticked",
      crop=lambda w: w.current.table)
def heatmap(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _split(ws, 300)
    ctx.open_table(w, MAP)
    ws.heat_chk.setChecked(True)
    return w


@shot("main-compare", "data/help", size=(1300, 1000),
      what="A tuned map with the stock file underneath, changed cells marked",
      crop=lambda w: w.current.table.parentWidget())
def compare(ctx):
    w, ws = _tuned_pair(ctx)
    _split(ws, 300)
    ctx.open_table(w, MAP, pin=True)
    _top(ws)
    return w


@shot("main-diff-list", "data/help", size=(820, 560),
      what="Edit > Show differences... against another software")
def diff_list(ctx):
    w, ws = _q_pair(ctx)
    ws.show_differences()
    dlg = ws._diff_dlg
    dlg.tbl.selectRow(7)
    return ctx.keep(dlg)


def _search(ctx, w, mode, query, scope="tab"):
    """Run a Find-panel search and wait for its results."""
    import time
    dock = w.search_dock
    dock.open()
    p = dock.panel
    p.mode.setCurrentIndex(p.mode.findData(mode))
    p.scope.setCurrentIndex(p.scope.findData(scope))
    p.edit.setText(query)
    p.run(commit=True)
    end = time.time() + 30
    while time.time() < end:
        ctx.settle(100)
        if not p._busy:
            break
    ctx.settle(300)
    return p


@shot("main-find", "data/help", size=(1280, 860),
      what="The Find panel: a text search, the first result shown")
def find(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _split(ws, 380)
    _search(ctx, w, "text", "max fuel boost")
    return w


@shot("main-find-value", "data/help", size=(1280, 860),
      what="The Find panel: a value search, the matching cells highlighted")
def find_value(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _split(ws, 380)
    ctx.open_table(w, MAP)
    p = _search(ctx, w, "value", "20..22")
    for i in range(p.results.topLevelItemCount()):
        it = p.results.topLevelItem(i)
        if it.text(1) == MAP:
            p.results.setCurrentItem(it)
            break
    ctx.settle(200)
    return w


# -- tool windows ---------------------------------------------------------------
@shot("main-hex", "data/help", size=(900, 640),
      what="Tools > Hex editor with a comparison loaded")
def hex_editor(ctx):
    w, ws = _tuned_pair(ctx)
    e = ctx.entity(w, MAP)
    ws.open_hex()
    dlg = ws.tool("hex")
    i = dlg._jump_index.get(e.addr)
    if i is not None:
        dlg.jump.setCurrentIndex(i)
        dlg._jump_to(i)
    # the first changed byte of the edited block
    a = next(a for a in range(e.addr, e.addr + 400)
             if ws.primary.binfile.data[a] != ws.comparison.binfile.data[a])
    dlg._select_addr(a)
    return ctx.keep(dlg)


@shot("main-3d", "data/help", size=(1180, 600),
      what="3D surface of a map and its comparison")
def surface(ctx):
    w, ws = _q_pair(ctx)
    ctx.open_table(w, MAP)
    ws.open_surface()
    from ovtsuite import gui
    from PySide6 import QtWidgets
    dlg = next(d for d in QtWidgets.QApplication.topLevelWidgets()
               if isinstance(d, gui.Surface3DDialog) and d.isVisible())
    return ctx.keep(dlg)


def _saved_history(ctx):
    """The LQ file's project, its *Stage 1* branch saved three times (two of
    the versions named), with an unsaved edit on top."""
    from ovtsuite import history
    w = ctx.main_window(ctx.ref("edc16_lq"), branch="Stage 1")
    ws = _ws(w)
    steps = (("Checksums corrected", lambda: _bump(ws, MAP, 1.0001,
                                                   rows=range(0, 1),
                                                   cols=range(0, 1))),
             ("Injection duration +8%", lambda: _bump(ws, MAP)),
             ("", lambda: _bump(ws, MAP2, 1.1, rows=range(10, 16),
                                cols=range(0, 4))))
    for name, edit in steps:
        edit()
        if not ws.save():
            from help_screenshots import Skip
            raise Skip("the project could not be saved")
        ws = _ws(w)
        if name:
            history.set_label(ws.primary,
                              history.list_versions(ws.primary)[0]["id"], name)
    _bump(ws, MAP, 1.02, rows=range(0, 2), cols=range(0, 3))    # unsaved
    return w, ws


@shot("main-version-history", "data/help", size=(820, 420),
      what="Edit > Version history...")
def version_history(ctx):
    from ovtsuite import gui
    w, ws = _saved_history(ctx)
    ws.open_version_history()
    return _opened(ctx, gui.VersionHistoryDialog)


@shot("main-history-patch", "data/help", size=(640, 180),
      what="Edit > Generate patch from history...")
def history_patch(ctx):
    from ovtsuite import gui
    w, ws = _saved_history(ctx)
    ws.open_history_patch()
    return _opened(ctx, gui.HistoryPatchDialog)


@shot("main-patch-builder", "data/help", size=(860, 560),
      what="Edit > Generate patch... in a tune's tab")
def patch_builder(ctx):
    from ovtsuite import gui
    w, ws = _tuned_pair(ctx)
    ws.open_patch_builder()
    return _opened(ctx, gui.PatchBuilderDialog)


def _patch_file(ctx, ws):
    """Save the tab's changes since the original (the comparison) as a
    .patch; return its path."""
    from ovtsuite import patch
    entries = patch.diff_entities(ws.comparison, ws.primary)
    pt = patch.new_patch(ws.comparison, ws.primary, entries)
    path = os.path.join(_tempdir(), "injection-and-torque.patch")
    pt.save(path)
    return path


@shot("main-patch-apply", "data/help", size=(880, 360),
      what="Edit > Apply patch... onto another software")
def patch_apply(ctx):
    from ovtsuite import gui, patch
    w, ws = _tuned_pair(ctx)
    path = _patch_file(ctx, ws)
    tws = ctx.open_project(w, ctx.ref("edc16_q"))
    dlg = gui.PatchApplyDialog(tws.primary, patch.Patch.load(path),
                               os.path.basename(path), tws)
    return ctx.keep(dlg)


@shot("main-remove-patch", "data/help", size=(760, 260),
      what="Edit > Remove patch... after a patch was applied")
def remove_patch(ctx):
    from ovtsuite import gui, patch
    w, ws = _tuned_pair(ctx)
    path = _patch_file(ctx, ws)
    tws = ctx.open_project(w, ctx.ref("edc16_q"))
    patch.apply_patch(patch.Patch.load(path), tws.primary,
                      source_label=os.path.basename(path), label="apply patch")
    dlg = gui.RemovePatchDialog(tws.primary, tws)
    dlg.tbl.selectRow(0)
    return ctx.keep(dlg)


# -- tabs and sessions --------------------------------------------------------
@shot("main-tabs", "data/help", size=(1280, 120),
      what="Several file tabs, tinted by kind, one with a comparison",
      crop=lambda w: w.tabs.tabBar())
def tabs(ctx):
    w = ctx.main_window(ctx.ref("edc16_lq"), branch="Stage 1")
    _bump(_ws(w), MAP)                  # unsaved: the tab shows *
    for key in ("zf6hp19", "edc15_bd"):
        try:
            ctx.open_project(w, ctx.ref(key))
        except Exception:
            pass
    w.tabs.setCurrentIndex(0)
    return w


@shot("main-restore-projects", "data/help", size=(600, 240),
      what="Reopen projects, at startup")
def restore_projects(ctx):
    from ovtsuite import gui
    d = tempfile.mkdtemp()
    a6 = os.path.join(d, "A6 BHW swap.ovt")
    golf = os.path.join(d, "Golf IV.ovt")
    for p in (a6, golf):
        open(p, "wb").close()           # the prompt only checks they exist
    ref = "%s#ovt:%s/%s"
    entries = [
        {"primary": ref % (a6, "s1", "b2"), "platform": "edc16",
         "project": a6, "project_title": "A6 BHW swap",
         "vehicle": "Audi A6 (C6) 2006", "set_branch": "ECU \u00b7 Stage 1"},
        {"primary": ref % (a6, "s2", "b1"), "platform": "zf6hp",
         "project": a6, "project_title": "A6 BHW swap",
         "vehicle": "Audi A6 (C6) 2006", "set_branch": "TCU \u00b7 Stock"},
        {"primary": ref % (golf, "s1", "b1"), "platform": "edc15",
         "project": golf, "project_title": "Golf IV",
         "vehicle": "VW Golf (1J) 2001", "set_branch": "ECU \u00b7 Stock"},
        {"primary": ctx.ref("edc16_q"), "platform": "edc16",
         "label": "ECU  038906016Q.Bin"},
        {"primary": os.path.join(d, "gone", "Passat.ovt#ovt:s1/b1"),
         "platform": "edc16", "project": os.path.join(d, "gone", "Passat.ovt"),
         "project_title": "Passat", "set_branch": "ECU \u00b7 Stock"},
    ]
    dlg = gui.RestoreProjectsDialog(entries)
    return ctx.keep(dlg)


@shot("main-start", "data/help", size=(1000, 520),
      what="The start page of an empty tab")
def start_page(ctx):
    from ovtsuite import recent
    d = os.path.join(tempfile.gettempdir(), "OVT-Suite help", "Projects")
    os.makedirs(d, exist_ok=True)
    shown = []
    for name in ("A6 BHW swap.ovt", "Passat B5 AFN.ovt", "Golf IV ALH.ovt"):
        p = os.path.join(d, name)
        open(p, "wb").close()
        shown.append(p)
    # the developer's own recent list is left alone: the page reads these
    # until the shot is taken (ctx.reset "closes" the restorer)
    real = recent.load_recent_projects

    class _Restore:
        def close(self):
            recent.load_recent_projects = real

    recent.load_recent_projects = lambda: list(shown)
    ctx.keep(_Restore())
    w = ctx.main_window()
    _ws(w)._start_page.refresh()
    return w


@shot("main-unsaved", "data/help", size=(520, 170),
      what="Closing the window with unsaved changes")
def unsaved(ctx):
    from ovtsuite import gui, gui_project
    w, ws = _tuned_pair(ctx)
    ws2 = ctx.open_project(w, ctx.ref("edc16_q"))
    _bump(ws2, MAP, 1.05, rows=range(0, 2), cols=range(0, 2))
    # what closing the window lists (MainWindow.closeEvent)
    dirty = [t for t in w.workspaces() if t.is_dirty()]
    dirty += gui_project.unsaved_entries(w)
    dlg = gui.UnsavedDialog(dirty, w)
    return ctx.keep(dlg)


@shot("main-file-info", "data/help", size=(640, 470),
      what="File > File information...")
def file_info(ctx):
    from PySide6 import QtWidgets
    w = ctx.main_window(ctx.ref("edc16_lq"))
    ws = _ws(w)
    _SHOW_FILE_INFO(ws)
    dlg = next(d for d in w.findChildren(QtWidgets.QDialog)
               if d.windowTitle().startswith("File information"))
    return ctx.keep(dlg)
