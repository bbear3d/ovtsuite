# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The File menu in the project model: open, import, compare, drop.

Everything the user works on is a project (``.ovt``):

* **Open project...** / **Open Recent** open projects -- nothing else;
* **Import** brings files in, *into this project* or *into a new project*:
  a binary or container (``.bin``, ``.sgo``, ``.frf``, ``.dim``, an old
  ``.edc16`` / ``.6hp`` ... or ``.edc15bak``) becomes a set, opened in a new
  tab -- or, with the *in this tab* entries, in the tab in front, which is
  closed first; a WinOLS project brings its binary and maps; a definition
  (XDF, A2L, KP, DAM) is attached to a binary;
* **Import as comparison** reads any of those as the comparison -- a project
  asks which of its binaries, branch and version;
* a file dropped on the window does what its kind says (a project opens; a
  binary is imported, into this project or a new one).

A project another OVT-Suite window has open is refused here (each open
project holds a lock), so two windows never save over each other.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import gui_project as _gp

#: File > Import's file types
IMPORT_FILTER = (
    "Importable files (*.bin *.ori *.hex *.mpc *.sgo *.frf *.dim *.ols "
    "*.edc16 *.edc15 *.6hp *.8hp *.vdo *.edc15bak *.xdf *.a2l *.kp *.dam);;"
    "Binaries and containers (*.bin *.ori *.hex *.mpc *.sgo *.frf *.dim "
    "*.edc16 *.edc15 *.6hp *.8hp *.vdo *.edc15bak);;"
    "WinOLS projects (*.ols);;"
    "Definitions (*.xdf *.a2l *.kp *.dam);;"
    "All files (*)")
PROJECT_FILTER = "OVT projects (*.ovt);;All files (*)"
COMPARE_FILTER = (
    "Projects and binaries (*.ovt *.bin *.ori *.sgo *.frf *.dim *.edc16 "
    "*.edc15 *.6hp *.8hp *.vdo);;All files (*)")


# ------------------------------------------------------------------ kinds
def classify(path: str) -> str:
    """``project``, ``ols``, ``definition`` or ``binary`` -- by content."""
    if _gp.is_project(path):
        return "project"
    kind = _gp.definition_kind(path)
    if kind == "ols":
        return "ols"
    if kind in ("xdf", "a2l", "kp", "dam"):
        return "definition"
    return "binary"


def _pick(win, title, filt, many=False):
    from .recent import get_last_dir, set_last_dir
    if many:
        paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
            win, title, get_last_dir(), filt)
    else:
        p, _f = QtWidgets.QFileDialog.getOpenFileName(
            win, title, get_last_dir(), filt)
        paths = [p] if p else []
    if paths:
        set_last_dir(paths[0])
    return paths


# --------------------------------------------------------------- projects
def open_project_dialog(win) -> bool:
    """File > Open project... (Ctrl+O)."""
    paths = _pick(win, "Open project", PROJECT_FILTER)
    return bool(paths) and open_any(win, paths[0])


def open_any(win, path: str) -> bool:
    """A file given to the window -- the command line, a double-clicked
    project, a drop, an old recent entry: a project opens; anything else is
    imported into a new project."""
    if not path:
        return False
    if _gp.parse_ref(path) is not None:
        return win.open_path(path)
    if not os.path.exists(path):
        QtWidgets.QMessageBox.information(
            win, "Open", "%s is no longer on disk." % path)
        from .recent import remove_recent
        remove_recent(path)
        return False
    if classify(path) == "project":
        ok = _gp.open_project(win, path)
        if ok:
            _info_on_open(win)
        return ok
    return import_files(win, [path], target="new")


def _info_on_open(win, ws=None) -> None:
    """File information for the tab a file just opened in (unless the user
    turned that off -- gui_fileinfo)."""
    ws = ws or win.current
    if ws is not None and ws.primary is not None:
        try:
            ws.show_file_info(loaded=True)
        except Exception:
            pass


def fill_recent_menu(win, menu) -> None:
    """File > Open Recent: the projects opened lately."""
    from .recent import clear_recent, load_recent_projects
    menu.clear()
    items = load_recent_projects()
    if not items:
        a = menu.addAction("(no recent projects)")
        a.setEnabled(False)
        return
    for p in items:
        act = menu.addAction(os.path.splitext(os.path.basename(p))[0])
        act.setToolTip(p)
        act.triggered.connect(lambda _c=False, path=p: open_any(win, path))
    menu.addSeparator()
    menu.addAction("Clear list", clear_recent)


# ----------------------------------------------------------------- import
def _current_doc(win):
    ref = getattr(win.current, "project_ref", None)
    return ref.doc if ref is not None else None


def import_dialog(win, target: str = "new", in_tab: bool = False) -> bool:
    """File > Import > Into a new project / Into this project (``target``
    ``"new"`` / ``"current"``), optionally into the tab in front."""
    where = ("this project" if target == "current" else "a new project")
    paths = _pick(win, "Import into %s" % where, IMPORT_FILTER, many=True)
    return bool(paths) and import_files(win, paths, target=target,
                                        in_tab=in_tab)


def import_definition_dialog(win) -> bool:
    """File > Import > Definition file for this binary..."""
    ref = getattr(win.current, "project_ref", None)
    if ref is None:
        return False
    from .definitions import FILTER
    paths = _pick(win, "Definition for %s" % ref.set.name, FILTER)
    return bool(paths) and _gp.attach_definition(win, ref.doc, ref.sid,
                                                 paths[0])


def _attach_definitions(win, doc, made, defs) -> List[str]:
    """Attach each definition to the binary it belongs to: the only one
    imported with it, or the one of the same name. ``made`` is
    ``[(path, set)]``. Returns the definitions left over."""
    from .project import importers as _imp
    left = []
    for d in defs:
        stem = os.path.splitext(os.path.basename(d))[0].lower()
        target = made[0][1] if len(made) == 1 else next(
            (s for p, s in made
             if os.path.splitext(os.path.basename(p))[0].lower() == stem), None)
        main = _gp._sb._main_member(doc.project, target.id) \
            if target is not None else None
        with open(d, "rb") as fh:
            data = fh.read()
        kind = _imp.definition_type(data)
        if main is None or kind is None:
            left.append(d)
            continue
        doc.project.add_definition(target.id, main.id, os.path.basename(d),
                                   data, kind)
    return left


def _import_each(win, proj, paths):
    """``[(path, set)]`` of the files that went in; the rest are reported."""
    from .project import importers as _imp
    made, problems = [], []
    for path in paths:
        try:
            _p, s = _imp.import_file(path, proj)
            made.append((path, s))
        except Exception as ex:
            problems.append("%s: %s" % (os.path.basename(path), ex))
    if problems:
        QtWidgets.QMessageBox.warning(win, "Import", "\n".join(problems))
    return made


def import_files(win, paths: List[str], target: str = "new",
                 in_tab: bool = False) -> bool:
    """Import ``paths`` (see the module doc). ``target`` ``"current"`` adds
    to the active tab's project (a new one when there is none)."""
    from .project import Project
    paths = [p for p in paths if p]
    if not paths:
        return False
    kinds = {p: classify(p) for p in paths}
    ok = False
    for p in [p for p in paths if kinds[p] == "project"]:
        ok = _gp.open_project(win, p) or ok
    rest = [p for p in paths if kinds[p] != "project"]
    if not rest:
        return ok
    defs = [p for p in rest if kinds[p] == "definition"]
    bins = [p for p in rest if kinds[p] != "definition"]
    doc = _current_doc(win) if target == "current" else None
    cur = win.current
    if not bins:
        # definitions alone: onto this tab's binary, or ask which binary
        ref = getattr(cur, "project_ref", None)
        if target == "current" and ref is not None:
            done = False
            for d in defs:
                done = _gp.attach_definition(win, ref.doc, ref.sid, d) or done
            return done or ok
        for d in defs:
            ok = _gp.open_definition(win, d, _gp.definition_kind(d) or "") or ok
        return ok
    # the tab in front goes first when the import is to replace it
    index = None
    if in_tab and cur is not None and not cur.is_empty():
        # importing into this tab's own project keeps the project open: ask
        # about the tab only, not about closing the project
        cur.__dict__["_keep_project"] = doc is not None
        try:
            if not cur.confirm_close():
                return ok
        finally:
            cur.__dict__.pop("_keep_project", None)
        index = win.tabs.indexOf(cur)
        if doc is not None:
            # its project stays open for the import, even if this was its
            # last tab
            doc.__dict__["_hold"] = True
        try:
            win._remove_workspace(cur)
        finally:
            if doc is not None:
                doc.__dict__.pop("_hold", None)
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        new = doc is None
        if new:
            name = os.path.splitext(os.path.basename(bins[0]))[0]
            proj = Project(name)
        else:
            proj = doc.project
        made = _import_each(win, proj, bins)
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()
    if not made:
        if doc is not None and not doc.tabs(win):
            _gp._drop_doc(win, doc)
        return ok
    sets = [s for _p, s in made]
    if new:
        doc = _gp.ProjectDoc(proj, None)
        _gp.docs(win).append(doc)
    left = _attach_definitions(win, doc, made, defs)
    if left:
        QtWidgets.QMessageBox.information(
            win, "Import", "Not attached (pick one binary with them, or use "
            "Import > Definition file for this binary):\n%s"
            % "\n".join(os.path.basename(x) for x in left))
    first = None
    for s in sets:
        t = _gp.open_branch(win, doc, s.id, s.active.id)
        first = first or t
    if first is None and not doc.tabs(win):
        _gp._drop_doc(win, doc)
        return ok
    if index is not None and first is not None:
        win.tabs.tabBar().moveTab(win.tabs.indexOf(first), index)
        win.show_workspace(first)
    if new:
        try:
            doc.saver.snapshot({}, wait=False)  # protected before any save
        except Exception:
            pass
    warn = getattr(proj.report, "warnings", None)
    msg = "Imported %s into %s" % (
        ", ".join(os.path.basename(b) for b in bins), doc.title)
    if warn:
        msg += " -- " + warn[0]
    if first is not None:
        first.statusBar().showMessage(msg, 10000)
    _gp.show_panel(win)
    _info_on_open(win, first)
    return True


# ------------------------------------------------------------ comparison
class VersionPicker(QtWidgets.QDialog):
    """Which binary of a project, which branch, which version: a tree of
    sets > branches > versions (newest first). Sets of another platform than
    ``platform`` are shown but can't be picked."""

    def __init__(self, project, title, parent=None, platform=None,
                 branches_only=False, text=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.project = project
        self.choice: Optional[Tuple[str, str, str]] = None
        v = QtWidgets.QVBoxLayout(self)
        lead = QtWidgets.QLabel(text or "Pick the binary and the version.")
        lead.setWordWrap(True)
        v.addWidget(lead)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Binary / branch / version", "Saved"])
        self.tree.setColumnWidth(0, 330)
        first = None
        from .gui import platform_badge
        for s in project.sets:
            badge, long_name, col = platform_badge(s.platform)
            si = QtWidgets.QTreeWidgetItem(["%s  %s" % (badge, s.name), ""])
            si.setToolTip(0, long_name)
            fits = platform is None or s.platform == platform
            if not fits:
                si.setText(0, si.text(0) + "   (another kind of unit)")
                si.setDisabled(True)
            self.tree.addTopLevelItem(si)
            for b in s.branches:
                star = "★ " if s.active is not None and b.id == s.active.id \
                    else ""
                bi = QtWidgets.QTreeWidgetItem(["%s%s" % (star, b.name), ""])
                bi.setData(0, QtCore.Qt.ItemDataRole.UserRole,
                           (s.id, b.id, b.head))
                si.addChild(bi)
                if branches_only:
                    if fits and first is None and star:
                        first = bi
                    continue
                for i, c in enumerate(s.lineage(b.head)):     # newest first
                    name = ("as read" if c.kind == "read" and c.parent is None
                            else c.label or c.kind)
                    if i == 0:
                        name += "  (latest)"
                    ci = QtWidgets.QTreeWidgetItem(
                        [name, (c.ts or "").replace("T", " ")[:16]])
                    ci.setData(0, QtCore.Qt.ItemDataRole.UserRole,
                               (s.id, b.id, c.id))
                    bi.addChild(ci)
                    if fits and first is None and star and i == 0:
                        first = ci
            si.setExpanded(fits)
            for j in range(si.childCount()):
                si.child(j).setExpanded(fits and si.child(j).text(0)
                                        .startswith("★"))
        v.addWidget(self.tree, 1)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self._ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.tree.currentItemChanged.connect(self._changed)
        self.tree.itemDoubleClicked.connect(
            lambda it, _c: self.accept() if self._value(it) else None)
        if first is not None:
            self.tree.setCurrentItem(first)
        self._changed(self.tree.currentItem(), None)
        self.resize(560, 420)

    @staticmethod
    def _value(it):
        return it.data(0, QtCore.Qt.ItemDataRole.UserRole) \
            if it is not None and not it.isDisabled() else None

    def _changed(self, it, _old):
        self.choice = self._value(it)
        self._ok.setEnabled(self.choice is not None)

    @staticmethod
    def only_choice(project, platform=None, branches_only=False):
        """The one possible pick when there is nothing to choose, else None."""
        cands = []
        for s in project.sets:
            if platform is not None and s.platform != platform:
                continue
            for b in s.branches:
                if branches_only:
                    cands.append((s.id, b.id, b.head))
                else:
                    cands += [(s.id, b.id, c.id) for c in s.lineage(b.head)]
        return cands[0] if len(cands) == 1 else None


def _project_for(win, path):
    """The open project at ``path``, or it loaded (not opened in a tab)."""
    from .project import Project
    doc = _gp._find_doc(win, path)
    if doc is not None:
        return doc.project, doc
    return Project.load(path), None


def import_comparison_dialog(win) -> bool:
    """File > Import as comparison...: a binary, or a version from a project."""
    cur = win.current
    if cur is None or cur.primary is None:
        return False
    paths = _pick(win, "Import as comparison", COMPARE_FILTER)
    return bool(paths) and compare_with_file(win, paths[0])


def compare_with_file(win, path: str) -> bool:
    """``path`` as the active tab's comparison: a project asks which of its
    binaries and versions; a recent ``<project>#ovt:<set>/<branch>@<version>``
    names one."""
    cur = win.current
    if cur is None or cur.primary is None:
        return False
    got = _gp.parse_ref(path)
    if got is not None:
        ovt, sid, rest = got
        bid, _at, cid = rest.partition("@")
        return compare_with_project(cur, ovt, sid, bid, cid or None)
    if classify(path) != "project":
        return win.open_comparison_path(path)
    try:
        proj, _doc = _project_for(win, path)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "Import as comparison",
                                       "%s could not be read: %s"
                                       % (os.path.basename(path), ex))
        return False
    plat = cur.platform()
    pick = VersionPicker.only_choice(proj, plat)
    if pick is None:
        if not any(s.platform == plat for s in proj.sets):
            QtWidgets.QMessageBox.information(
                win, "Import as comparison", "%s holds no binary of the kind "
                "in this tab." % os.path.basename(path))
            return False
        dlg = VersionPicker(proj, "Compare with %s" % (
            proj.name or os.path.basename(path)), win, platform=plat,
            text="Which binary of %s, and which version, to compare this tab "
                 "against?" % (proj.name or os.path.basename(path)))
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or not dlg.choice:
            return False
        pick = dlg.choice
    sid, bid, cid = pick
    return compare_with_project(cur, path, sid, bid, cid)


def _cmp_tmp(win) -> str:
    d = win.__dict__.get("_ovt_cmp_tmp")
    if not d or not os.path.isdir(d):
        d = tempfile.mkdtemp(prefix="ovt-cmp-")
        win.__dict__["_ovt_cmp_tmp"] = d
    return d


def compare_with_project(ws, path, sid, bid, cid=None) -> bool:
    """Version ``cid`` (the branch's latest when None) of set ``sid`` of the
    project at ``path`` as ``ws``'s comparison. The branch open in a tab,
    at its latest version, is offered live instead."""
    win = ws.win
    try:
        proj, doc = _project_for(win, path)
        s = proj.set(sid)
        b = s.branch(bid)
        cid = cid or b.head
        c = s.checkpoint(cid)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(ws, "Compare", str(ex))
        return False
    if c is None:
        QtWidgets.QMessageBox.information(ws, "Compare",
                                          "That version is not in the project.")
        return False
    if doc is not None and cid == b.head:
        for t in doc.tabs(win):
            if t is not ws and t.project_ref.sid == sid \
                    and t.project_ref.bid == bid:
                return ws.link_comparison(t)
    from .project import sessionbridge as _sb
    name = proj.name or os.path.splitext(os.path.basename(path))[0]
    ver = ("as read" if c.kind == "read" and c.parent is None
           else c.label or c.kind)
    label = "%s - %s - %s - %s" % (name, s.name, b.name, ver)
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        state = proj.state(sid, cid)
        ext, blob = _sb.working_file(
            proj, sid, state, defn=_sb.definition_for(proj, sid, bid, state))
        work = _sb.write_working_file(_cmp_tmp(win), label, ext, blob)
    except Exception as ex:
        QtWidgets.QApplication.restoreOverrideCursor()
        QtWidgets.QMessageBox.critical(ws, "Compare",
                                       "That version could not be rebuilt: %s"
                                       % ex)
        return False
    QtWidgets.QApplication.restoreOverrideCursor()
    try:
        comp = ws._load_session(work, title="Loading %s" % label)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(ws, "Compare", str(ex))
        return False
    _gp._apply_definition(proj, sid, comp)
    problem = ws.pair_problem(comp)
    if problem:
        QtWidgets.QMessageBox.information(ws, "Different platform", problem)
        return False
    comp.version_of = {"id": cid, "label": ver, "ts": c.ts, "path": None,
                       "project": os.path.abspath(path)}
    ws.set_comparison(comp)
    from .recent import add_recent_comparison
    add_recent_comparison("%s@%s" % (_gp.make_ref(path, sid, bid), cid))
    ws.statusBar().showMessage("Comparing against %s ▸ %s · %s (%s)."
                               % (name, s.name, b.name, ver), 8000)
    return True


def fill_recent_comparisons(win, menu) -> None:
    """File > Import recent as comparison: binaries and project versions
    compared lately."""
    from .recent import clear_recent_comparisons, load_recent_comparisons
    menu.clear()
    items = load_recent_comparisons()
    if not items:
        a = menu.addAction("(none)")
        a.setEnabled(False)
        return
    for p in items:
        got = _gp.parse_ref(p)
        if got is not None:
            text = "%s  (a version in the project)" % os.path.splitext(
                os.path.basename(got[0]))[0]
        else:
            text = os.path.basename(p)
        act = menu.addAction(text)
        act.setToolTip(_gp.real_path(p))
        act.triggered.connect(lambda _c=False, path=p:
                              compare_with_file(win, path))
    menu.addSeparator()
    menu.addAction("Clear comparison history", clear_recent_comparisons)


# ---------------------------------------------------------------- drops
def drop_paths(mime) -> List[str]:
    if mime is None or not mime.hasUrls():
        return []
    return [u.toLocalFile() for u in mime.urls()
            if u.isLocalFile() and os.path.isfile(u.toLocalFile())]


def handle_drop(win, paths: List[str], global_pos=None) -> bool:
    """Files dropped on the window: projects open; other files are imported
    -- straight into a new project, or, with a project in front, after a
    menu: into it, into a new one, or (one binary) as the comparison."""
    if not paths:
        return False
    projects = [p for p in paths if classify(p) == "project"]
    others = [p for p in paths if p not in projects]
    ok = False
    for p in projects:
        ok = open_any(win, p) or ok
    if not others:
        return ok
    doc = _current_doc(win)
    cur = win.current
    if doc is None and (cur is None or cur.primary is None):
        return import_files(win, others, target="new") or ok
    menu = QtWidgets.QMenu(win)
    names = ", ".join(os.path.basename(p) for p in others[:3])
    if len(others) > 3:
        names += ", ..."
    head = menu.addAction(names)
    head.setEnabled(False)
    menu.addSeparator()
    into = None
    if doc is not None:
        into = menu.addAction("Import into %s" % doc.title)
    new = menu.addAction("Import into a new project")
    cmp_ = None
    if len(others) == 1 and cur is not None and cur.primary is not None \
            and classify(others[0]) == "binary":
        cmp_ = menu.addAction("Compare with this tab")
    menu.setDefaultAction(into or new)
    act = menu.exec(global_pos or QtGui.QCursor.pos())
    if act is None:
        return ok
    if act is into:
        return import_files(win, others, target="current") or ok
    if act is new:
        return import_files(win, others, target="new") or ok
    if act is cmp_:
        return compare_with_file(win, others[0]) or ok
    return ok


# --------------------------------------------------------- project locks
def _lock_dir() -> str:
    base = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.TempLocation) \
        or tempfile.gettempdir()
    d = os.path.join(base, "ovtsuite-locks")
    os.makedirs(d, exist_ok=True)
    return d


#: locks this process holds: key -> [QLockFile, number of windows using it]
_HELD: dict = {}


def _locks(win) -> set:
    return win.__dict__.setdefault("_ovt_locks", set())


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(path))


def lock_project(win, path) -> bool:
    """Hold ``path`` for this window. False (and says so) when another
    OVT-Suite process holds it; windows of one process share it. A lock left
    by a crashed process is taken over (QLockFile checks the process)."""
    if not path:
        return True
    key = _key(path)
    mine = _locks(win)
    if key in mine:
        return True
    held = _HELD.get(key)
    if held is None:
        lf = QtCore.QLockFile(os.path.join(
            _lock_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest()
            + ".lock"))
        lf.setStaleLockTime(0)
        if not lf.tryLock(0):
            QtWidgets.QMessageBox.information(
                win, "Project in use",
                "%s is open in another %s window. Switch to that window to "
                "work on it -- two windows saving one project would overwrite "
                "each other's changes." % (os.path.basename(path), _app_name()))
            return False
        held = _HELD[key] = [lf, 0]
    held[1] += 1
    mine.add(key)
    return True


def unlock_project(win, path) -> None:
    if not path:
        return
    key = _key(path)
    mine = _locks(win)
    if key not in mine:
        return
    mine.discard(key)
    held = _HELD.get(key)
    if held is not None:
        held[1] -= 1
        if held[1] <= 0:
            held[0].unlock()
            del _HELD[key]


def unlock_all(win) -> None:
    """The window closes: let go of every project it held."""
    for key in list(_locks(win)):
        unlock_project(win, key)


def _app_name() -> str:
    from . import branding
    return branding.APP_NAME


__all__ = ["classify", "open_project_dialog", "open_any", "fill_recent_menu",
           "import_dialog", "import_files", "import_definition_dialog",
           "VersionPicker", "import_comparison_dialog", "compare_with_file",
           "compare_with_project", "fill_recent_comparisons", "drop_paths",
           "handle_drop", "lock_project", "unlock_project", "unlock_all",
           "IMPORT_FILTER"]
