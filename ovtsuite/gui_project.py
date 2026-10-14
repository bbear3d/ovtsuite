# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""``.ovt`` projects in the main window (docs/OVT_CONTAINER.md, section 10).

A project's binary sets (ECU, TCU, ...) open as tabs of their own, one per
set and branch, labelled with the project's name. Each tab edits its branch
through an ordinary :class:`~ovtsuite.session.TunerSession` opened from a
working file (:mod:`ovtsuite.project.sessionbridge`), so every editor and tool
works unchanged; saving commits the tab back to the project as a checkpoint
and writes the ``.ovt``.

* Opening a project opens the tabs it had open last time on this computer, or
  every set (on its active branch) the first time. Anything not open can be
  opened from the Project panel or File > Open from project.
* A project tab's comparison is the as-read original of its branch, unless
  another is picked.
* Save writes the whole project (every edited tab of it); Save this branch only
  commits just the current tab.
* Crash recovery: each open project keeps an auto-save entry
  (:mod:`ovtsuite.project.autosave`), discarded on a proper exit.

The window code reaches this module through a handful of hooks; everything
about projects lives here.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import branding
from .appsettings import settings as _settings
from .gui_autohide import AutoHideDock
from .project import Project, is_project
from .project import archive as _ar
from .project import autosave as _as
from .project import exporters as _ex
from .project import sessionbridge as _sb
from .project.project import HistoryError

#: a project tab's "path": the .ovt, then the set and branch it shows
REF_SEP = "#ovt:"
#: QSettings group: which tabs each project had open, keyed by project id
STATE_KEY = "projects/open_tabs/%s"
#: how often open projects are auto-saved for crash recovery (ms)
AUTOSAVE_MS = 60_000


# ------------------------------------------------------------------ refs
def make_ref(path: str, sid: str, bid: str) -> str:
    return "%s%s%s/%s" % (os.path.abspath(path), REF_SEP, sid, bid)


def parse_ref(ref) -> Optional[Tuple[str, str, str]]:
    """``(ovt path, set id, branch id)`` of a project tab's path, else None."""
    if not ref or REF_SEP not in str(ref):
        return None
    path, rest = str(ref).split(REF_SEP, 1)
    sid, _, bid = rest.partition("/")
    return path, sid, bid


def exists(path) -> bool:
    """``os.path.exists`` that also understands a project tab's path."""
    got = parse_ref(path)
    if got is not None:
        return os.path.exists(got[0])
    return bool(path) and os.path.exists(path)


def real_path(path):
    got = parse_ref(path)
    return got[0] if got is not None else path


# ------------------------------------------------------------- documents
class ProjectDoc:
    """One open project: the :class:`Project`, where it lives, its working
    files and its crash-recovery entry."""

    def __init__(self, project: Project, path: Optional[str]):
        self.project = project
        self.path = os.path.abspath(path) if path else None
        self.tmpdir = tempfile.mkdtemp(prefix="ovt-project-")
        self.saver = _as.AutoSaver(project)
        #: the information prompt for a new project was shown (or the project
        #: already existed) -- the first Save does not ask again
        self.info_asked = path is not None

    @property
    def title(self) -> str:
        if self.project.name:
            return self.project.name
        if self.path:
            return os.path.splitext(os.path.basename(self.path))[0]
        return "Untitled project"

    def tabs(self, win) -> list:
        return [ws for ws in win.workspaces()
                if getattr(ws, "project_ref", None) is not None
                and ws.project_ref.doc is self]

    def is_dirty(self, win) -> bool:
        return self.project.modified_since_save or any(
            ws.is_dirty() for ws in self.tabs(win))

    def close(self):
        """The project's last tab went, the user having been asked: drop the
        crash-recovery entry and the working files."""
        try:
            self.saver.discard()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class ProjectRef:
    """What a project tab shows: a branch of a set of a project."""

    def __init__(self, doc: ProjectDoc, sid: str, bid: str, workfile: str):
        self.doc = doc
        self.sid = sid
        self.bid = bid
        self.workfile = workfile

    @property
    def set(self):
        return self.doc.project.set(self.sid)

    @property
    def branch(self):
        return self.set.branch(self.bid)

    def ref(self) -> Optional[str]:
        return make_ref(self.doc.path, self.sid, self.bid) if self.doc.path \
            else None


def docs(win) -> List[ProjectDoc]:
    if "_ovt_docs" not in win.__dict__:
        win.__dict__["_ovt_docs"] = []
    return win.__dict__["_ovt_docs"]


def doc_of(ws) -> Optional[ProjectDoc]:
    ref = getattr(ws, "project_ref", None)
    return ref.doc if ref is not None else None


def _find_doc(win, path) -> Optional[ProjectDoc]:
    key = os.path.normcase(os.path.abspath(path))
    for d in docs(win):
        if d.path and os.path.normcase(d.path) == key:
            return d
    return None


# --------------------------------------------------------------- labels
def tab_label(ws, base: str) -> str:
    """``A6 ▸ ECU · Stage 1  …`` -- the project and set in front of what the
    tab would say anyway."""
    ref = ws.project_ref
    try:
        b = ref.branch
        what = "%s · %s" % (ref.set.name, b.name if b else "?")
    except KeyError:
        what = "?"
    badge = base.split("  ", 1)[0]
    rest = base.split("  ↔  ", 1)
    dirty = "*" if ws.is_dirty() else ""
    out = "%s  %s ▸ %s%s" % (badge, ref.doc.title, what, dirty)
    if len(rest) > 1:
        out += "  ↔  " + rest[1]
    return out


def window_title(ws) -> str:
    """``A6 ▸ ECU · Stage 1 *`` -- the project, set and branch in front, and
    whether the project has anything unsaved."""
    ref = ws.project_ref
    doc = ref.doc
    return "%s ▸ %s · %s%s" % (doc.title, ref.set.name, ref.branch.name,
                              " *" if doc.is_dirty(ws.win) else "")


def tab_tooltip(ws, base: str) -> str:
    ref = ws.project_ref
    lines = base.split("\n")
    where = ref.doc.path or "not saved yet"
    lines = [l for l in lines if not l.startswith("Primary:")]
    lines.insert(1, "Project: %s  (%s)" % (ref.doc.title, where))
    vehicle = ref.doc.project.vehicle()
    if vehicle:
        lines.insert(2, "Vehicle: %s" % vehicle)
    try:
        lines.insert(3 if vehicle else 2,
                     "Set: %s, branch %s" % (ref.set.name, ref.branch.name))
        ft = flashed_text(ref.doc.project, ref.sid)
        if ft:
            lines.append(ft)
    except (KeyError, AttributeError):
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------- opening
def open_project(win, path, new_tab=False) -> bool:
    """Open the project at ``path``: the tabs it had open last time, or every
    set the first time. A project already open just shows its tabs."""
    there = _find_doc(win, path)
    if there is not None:
        tabs = there.tabs(win)
        if tabs:
            win.show_workspace(tabs[0])
            return True
        doc = there
    else:
        from .gui_files import lock_project
        if not lock_project(win, path):
            return False
        from .gui_files import unlock_project
        try:
            proj = Project.load(path)
        except _ar.UnsupportedFeature as ex:
            unlock_project(win, path)
            QtWidgets.QMessageBox.warning(win, "Open project", str(ex))
            return False
        except Exception as ex:
            unlock_project(win, path)
            QtWidgets.QMessageBox.critical(
                win, "Open project", "%s could not be opened:\n%s"
                % (os.path.basename(path), ex))
            return False
        doc = ProjectDoc(proj, path)
        docs(win).append(doc)
        _report_damage(win, doc)
    from .recent import add_recent
    add_recent(doc.path)
    targets = _last_tabs(doc) or [(s.id, s.active.id) for s in doc.project.sets
                                  if s.active is not None]
    opened = 0
    for sid, bid in targets:
        if open_branch(win, doc, sid, bid) is not None:
            opened += 1
    if not opened and not doc.tabs(win):
        if not doc.project.sets:
            QtWidgets.QMessageBox.information(
                win, "Open project", "%s holds no binaries yet." % doc.title)
        _drop_doc(win, doc)
        return False
    remember(win, doc)
    show_panel(win)
    return True


def _report_damage(win, doc):
    rep = doc.project.report
    bits = []
    if rep.damaged:
        bits.append("%d damaged entr%s" % (len(rep.damaged),
                                           "y" if len(rep.damaged) == 1 else "ies"))
    if rep.missing:
        bits.append("%d missing entr%s" % (len(rep.missing),
                                           "y" if len(rep.missing) == 1 else "ies"))
    if rep.signature.status == "invalid":
        bits.append("an invalid signature")
    if bits:
        QtWidgets.QMessageBox.warning(
            win, "Project damaged",
            "%s has %s. Everything that does not depend on the damaged data "
            "opens normally; the history panel marks versions that can't be "
            "rebuilt.\n\n%s" % (doc.title, " and ".join(bits),
                                "\n".join((rep.damaged + rep.missing)[:12])))


def open_branch(win, doc, sid, bid, ws=None):
    """A tab showing branch ``bid`` of set ``sid`` (an open one is brought
    forward). Returns the tab, or None."""
    for t in doc.tabs(win):
        if t.project_ref.sid == sid and t.project_ref.bid == bid:
            win.show_workspace(t)
            return t
    if ws is None:
        cur = win.current
        ws = cur if (cur is not None and cur.is_empty()) else win._add_workspace()
    if not load_into(ws, doc, sid, bid):
        if ws.is_empty() and win.tabs.count() > 1:
            win._remove_workspace(ws, remember=False)
        return None
    remember(win, doc)
    refresh_panel(win)
    return ws


def load_into(ws, doc, sid, bid) -> bool:
    """Load branch ``bid`` of set ``sid`` into tab ``ws``."""
    proj = doc.project
    try:
        s = proj.set(sid)
        b = s.branch(bid)
        state = proj.state(sid, b.head)
    except (KeyError, AttributeError, HistoryError) as ex:
        QtWidgets.QMessageBox.warning(
            ws, "Open from project", "That branch can't be opened: %s" % ex)
        return False
    log = _sb.history_text(proj, sid, b.head)
    ext, data = _sb.working_file(proj, sid, state, defn=proj.cache(sid, bid),
                                 log=log)
    work = _sb.write_working_file(doc.tmpdir, "%s - %s" % (doc.title, s.name),
                                  ext, data, log=log)
    try:
        loaded = ws._load_session(work, title="Opening %s" % s.name)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(ws, "Open from project", str(ex))
        return False
    why = _apply_definition(proj, sid, loaded)
    quiet = getattr(ws, "_quiet", False)
    ws._quiet = True                    # one project, not one popup per set
    try:
        # adopt_primary takes it up (and lets go of a project shown before)
        ws._pending_project_ref = ProjectRef(doc, sid, bid, work)
        ws.adopt_primary(loaded, work)
    finally:
        ws._quiet = quiet
    if not quiet and getattr(loaded, "support", "full") not in ("full", "definition"):
        # what the binary is and which module would support it: once per set
        # while the project is open (not for every tab, nor at start-up)
        told = doc.__dict__.setdefault("_limited_told", set())
        if sid not in told:
            told.add(sid)
            QtCore.QTimer.singleShot(
                0, lambda: ws.primary is loaded
                and ws._explain_limited_support(loaded))
    from .recent import remove_recent
    remove_recent(work)                 # the working file is never "recent"
    ws._rebuild_recent()
    _name(ws)
    root = s.root_of(b.head)
    if root is not None:
        compare_with_checkpoint(ws, root.id, quiet=True)
    ws.win._workspace_changed(ws)
    ws.statusBar().showMessage(why or
        "Opened %s ▸ %s · %s" % (doc.title, s.name, b.name), 8000)
    return True


def _apply_definition(proj, sid, session) -> str:
    """A packaged definition gives an unrecognised binary its parameter tree
    (ovtsuite.xdf_session). Returns a note for the status bar ("" if none)."""
    main = _sb._main_member(proj, sid)
    if main is None or not main.definition:
        return ""
    data = proj.definition_bytes(sid, main.id)
    if not data:
        return "The packaged definition is missing from the project."
    from . import xdf_session
    try:
        if getattr(session, "support", "") == "full" and \
                main.definition.get("use") != "instead":
            # a module defines it: the file's tables go beside the module's
            # (their own group), never instead of them
            label = os.path.basename(main.definition.get("path", "")) or None
            xdf_session.add_definition(session, data,
                                       main.definition.get("type"), label=label)
        else:
            xdf_session.apply_definition(session, data,
                                         main.definition.get("type"))
    except xdf_session.Unsupported as ex:
        return str(ex)
    except Exception as ex:
        return "The packaged definition could not be read: %s" % ex
    return getattr(session, "definition_notes", "") + "."


def _session_at(ws, cid, label):
    """A TunerSession of checkpoint ``cid`` of the tab's set.

    Another version of the same set has the same layout, so it shares the
    tab's own definition (``TunerSession.sibling``) instead of identifying
    and matching the image again -- the comparison costs no second load.
    Only a version of another size, or a platform whose definition can't
    be shared, is loaded in full."""
    ref = ws.project_ref
    proj = ref.doc.project
    state = proj.state(ref.sid, cid)
    main = _sb._main_member(proj, ref.sid)
    data = state.get(main.id) if main is not None else None
    if data is not None and ws.primary is not None:
        try:
            return ws.primary.sibling(bytes(data), label)
        except Exception:
            pass                        # load it in full below
    ext, blob = _sb.working_file(proj, ref.sid, state,
                                 defn=proj.cache(ref.sid, ref.bid))
    path = _sb.write_working_file(ref.doc.tmpdir, label, ext, blob)
    sess = ws._load_session(path, title="Loading %s" % label)
    _apply_definition(proj, ref.sid, sess)
    sess.binfile.name = label           # not the working file's name
    return sess


def compare_with_checkpoint(ws, cid, quiet=False) -> bool:
    """One of the set's checkpoints as the tab's comparison."""
    ref = ws.project_ref
    s = ref.set
    c = s.checkpoint(cid)
    if c is None:
        return False
    # the root is what a tab starts out compared with: short, so the tab
    # label keeps room for the tab's own name
    label = "%s - %s" % (s.name, ("as read" if c.kind == "read" else "original")
                         if c.parent is None else c.label or c.kind)
    try:
        comp = _session_at(ws, cid, label)
    except Exception as ex:
        if not quiet:
            QtWidgets.QMessageBox.critical(
                ws, "Compare", "That version could not be rebuilt: %s" % ex)
        return False
    comp.version_of = {"id": cid, "label": c.label or c.kind, "ts": c.ts,
                       "path": None}
    ws.set_comparison(comp)
    if not quiet:
        ws.statusBar().showMessage(
            "Comparing against \"%s\" (%s)." % (c.label, c.ts.replace("T", " ")),
            8000)
    return True


def load_ref(ws, path) -> bool:
    """``Workspace._load_primary_into`` for a project tab's path (reopening a
    tab from history or from last session)."""
    got = parse_ref(path)
    if got is None:
        return False
    ovt, sid, bid = got
    win = ws.win
    doc = _find_doc(win, ovt)
    if doc is None:
        from .gui_files import lock_project, unlock_project
        if not lock_project(win, ovt):
            return False
        try:
            doc = ProjectDoc(Project.load(ovt), ovt)
        except Exception as ex:
            unlock_project(win, ovt)
            QtWidgets.QMessageBox.critical(ws, "Open project", str(ex))
            return False
        docs(win).append(doc)
    for t in doc.tabs(win):
        if t is not ws and t.project_ref.sid == sid and t.project_ref.bid == bid:
            win.show_workspace(t)
            return False
    ok = load_into(ws, doc, sid, bid)
    if ok:
        remember(win, doc)
        # the panel comes with the project -- reopened at startup or from
        # history too, collapsed again if it was (it was left hidden)
        show_panel(win)
    elif not doc.tabs(win):
        _drop_doc(win, doc)
    return ok


# --------------------------------------------------------- remembering
def _last_tabs(doc) -> List[Tuple[str, str]]:
    try:
        raw = _settings().value(STATE_KEY % doc.project.uid, "")
        items = json.loads(raw) if raw else []
    except Exception:
        return []
    out = []
    for it in items:
        try:
            s = doc.project.set(it["set"])
            if s.branch(it["branch"]) is not None:
                out.append((s.id, it["branch"]))
        except (KeyError, TypeError):
            continue
    return out


def remember(win, doc):
    """Which of ``doc``'s tabs are open, for next time it is opened."""
    tabs = [{"set": t.project_ref.sid, "branch": t.project_ref.bid}
            for t in doc.tabs(win)]
    if not tabs:
        return                          # closing the last tab keeps the list
    try:
        _settings().setValue(STATE_KEY % doc.project.uid, json.dumps(tabs))
    except Exception:
        pass


def session_project(ws) -> dict:
    """What last session's list keeps about a project tab's project (so the
    startup prompt can name it without opening it)."""
    ref = getattr(ws, "project_ref", None)
    if ref is None:
        return {}
    doc = ref.doc
    return {"project": doc.path, "project_title": doc.title,
            "vehicle": doc.project.vehicle(),
            "set_branch": "%s · %s" % (ref.set.name, ref.branch.name)}


def session_groups(entries) -> List[dict]:
    """Last session's tabs (``MainWindow.tab_session``) as the projects (and
    loose files) they belong to, in the order first seen: ``kind``
    ("project" or "file"), ``path``, ``title``, ``vehicle``, ``tabs`` (what
    each tab showed), ``indexes`` (into ``entries``) and ``ok`` (it can be
    reopened)."""
    groups: List[dict] = []
    by_path = {}
    for i, ent in enumerate(entries):
        primary = ent.get("primary")
        got = parse_ref(primary)
        if got is not None or ent.get("project"):
            path = ent.get("project") or got[0]
            g = by_path.get(os.path.normcase(os.path.abspath(path)))
            if g is None:
                g = {"kind": "project", "path": path,
                     "title": ent.get("project_title")
                     or os.path.splitext(os.path.basename(path))[0],
                     "vehicle": ent.get("vehicle", ""), "tabs": [],
                     "indexes": [], "ok": os.path.exists(path),
                     "platforms": []}
                by_path[os.path.normcase(os.path.abspath(path))] = g
                groups.append(g)
            label = ent.get("set_branch") or (ent.get("label") or "").split(
                "▸ ", 1)[-1]
            g["tabs"].append(label.strip())
            g["indexes"].append(i)
            g["platforms"].append(ent.get("platform"))
        else:
            groups.append({
                "kind": "file", "path": primary,
                "title": ent.get("label") or os.path.basename(primary or ""),
                "vehicle": "", "tabs": [], "indexes": [i],
                "ok": bool(primary) and os.path.exists(primary),
                "platforms": [ent.get("platform")]})
    return groups


# ----------------------------------------------------------------- saving
def _commit(ws, label="") -> bool:
    """Commit the tab's edits to its project (checksums corrected exactly as
    a file save corrects them). The .ovt is not written here."""
    from . import changelog as _cl
    ref = ws.project_ref
    proj = ref.doc.project
    head = ref.branch.head
    ws._sync_eeprom_to_primary()
    ws.primary.save(ref.workfile)
    images, defn = _sb.read_back(proj, ref.sid, ref.workfile)
    lines, named, renames = _sb.read_log(proj, ref.sid, head,
                                        _cl.read_text(ref.workfile))
    for cid, name in renames.items():
        proj.set_checkpoint_label(ref.sid, cid, name)
    kind, auto = "save", "save"
    patches = [l for l in lines if l.startswith("PATCH ")]
    if patches:
        kind = "patch"
        try:
            src = json.loads(patches[-1][6:]).get("source", "")
            # an accepted AI proposal is its own version: "AI: <title>"
            auto = src if src.startswith("AI: ") else "patch: %s" % src
        except ValueError:
            auto = "patch"
    proj.commit(ref.sid, ref.bid, images, label=label or named or auto,
                kind=kind, log=lines)
    if defn:
        proj.set_cache(ref.sid, ref.bid, defn)
    _resync_log(ws)
    _name(ws)
    return True


def _name(ws):
    """The tab's file name as dialogs and the title bar show it: the project
    and set, not the working file's."""
    ref = ws.project_ref
    if ref is not None and ws.primary is not None:
        try:
            ws.primary.binfile.name = "%s - %s" % (ref.doc.title, ref.set.name)
        except KeyError:
            pass


def _resync_log(ws):
    """The working file's change log = the project's history again (ids are
    checkpoint ids), so the session's version list matches the project."""
    from . import changelog as _cl
    ref = ws.project_ref
    try:
        _cl.rewrite(ref.workfile, _sb.history_text(
            ref.doc.project, ref.sid, ref.branch.head))
    except Exception:
        pass


def after_history_dialog(ws):
    """Version history renamed checkpoints in the working file's log: carry
    the names into the project."""
    from . import changelog as _cl
    ref = ws.project_ref
    proj = ref.doc.project
    try:
        _l, _n, renames = _sb.read_log(proj, ref.sid, ref.branch.head,
                                       _cl.read_text(ref.workfile))
    except Exception:
        return
    for cid, name in renames.items():
        proj.set_checkpoint_label(ref.sid, cid, name)
    if renames:
        _resync_log(ws)
        ws.win._workspace_changed(ws)
        refresh_panel(ws.win)


def merge_versions(win, doc, sid, cids, label="") -> str:
    """Merge adjacent saved versions of set ``sid`` into one
    (``Project.merge_checkpoints``); every open tab of the set sees the new
    history. Returns the merged version's id; ValueError with the reason
    when they cannot be merged."""
    for t in doc.tabs(win):
        if t.project_ref.sid == sid:
            after_history_dialog(t)         # names typed in the dialog first
    c = doc.project.merge_checkpoints(sid, list(cids), label=label)
    for t in doc.tabs(win):
        if t.project_ref.sid == sid:
            _resync_log(t)
            win._workspace_changed(t)
    refresh_panel(win)
    return c.id


def _ask_path(win, name, current) -> Optional[str]:
    """Where to save a project (``.ovt`` only: raw and native formats are
    exports)."""
    from .recent import get_last_dir, set_last_dir
    start = current or os.path.join(get_last_dir(), name + _ar.EXT)
    path, _f = QtWidgets.QFileDialog.getSaveFileName(
        win, "Save project", start, "OVT project (*%s)" % _ar.EXT)
    if not path:
        return None
    if not path.lower().endswith(_ar.EXT):
        path += _ar.EXT
    set_last_dir(path)
    return path


def _ask_project_path(win, doc) -> Optional[str]:
    return _ask_path(win, doc.title, doc.path)


def save_selection(win, doc, tabs) -> bool:
    """Save project ``doc`` once, committing only ``tabs`` (the window's
    exit list); an empty list saves just the project's own changes."""
    anchor = tabs[0] if tabs else (doc.tabs(win)[0] if doc.tabs(win) else None)
    if anchor is None:
        try:
            if doc.path is None:
                return False
            from . import gui_disksync as _dsg
            if not _dsg.write(win, doc):
                return False
            doc.saver.saved()
            return True
        except Exception as ex:
            QtWidgets.QMessageBox.critical(win, "Save failed", str(ex))
            return False
    win.show_workspace(anchor)
    return save(anchor, only=list(tabs))


def save(ws, tab_only=False, path=None, only=None) -> bool:
    """Save the tab's project: every edited tab of it, or with ``tab_only``
    just this one, or the tabs in ``only`` (the others stay unsaved). True
    once it is on disk."""
    win = ws.win
    doc = ws.project_ref.doc
    if path is None and doc.path is None:
        path = _ask_project_path(win, doc)
        if path is None:
            return False
        if not doc.info_asked:
            edit_info(win, doc, new=True)
    from . import gui_disksync as _dsg
    force = False
    if path is None or os.path.abspath(path) == doc.path:
        # changed on disk by another program: ask (merge / overwrite / a
        # copy) before anything is committed, so Cancel changes nothing
        force = _dsg.ensure_current(win, doc)
        if force is None or ws.project_ref is None:
            return False
    if only is not None:
        tabs = [t for t in only if getattr(t, "project_ref", None) is not None
                and t.project_ref.doc is doc]
    else:
        tabs = [ws] if tab_only else [t for t in doc.tabs(win) if t.is_dirty()]
        if ws not in tabs and ws.is_dirty():
            tabs.append(ws)
    try:
        for t in tabs:
            if t.is_dirty():
                why = t.primary.save_refusal(t.project_ref.workfile)
                if why:
                    raise ValueError(why)
                _commit(t)
        for t in doc.tabs(win):
            keep_definition(t)          # an unedited tab's, too
        if path is not None and os.path.abspath(path) != doc.path:
            from .gui_files import lock_project, unlock_project
            if not lock_project(win, path):
                return False
            unlock_project(win, doc.path)
            doc.path = os.path.abspath(path)
        if not _dsg.write(win, doc, force=force):
            return False
        doc.__dict__["_cache_dirty"] = False
    except Exception as ex:
        ws._save_failed(doc.path or doc.title, ex)
        return False
    from .recent import add_recent
    add_recent(doc.path)
    ws._rebuild_recent()
    if any(t.is_dirty() for t in doc.tabs(win)):
        autosave(win, doc)              # the other tabs are still unsaved
    else:
        doc.saver.saved()
    for t in tabs:
        t._after_save()
        win._workspace_changed(t)
    for t in doc.tabs(win):
        win._workspace_changed(t)
    remember(win, doc)
    refresh_panel(win)
    return True


def save_as(ws) -> bool:
    doc = ws.project_ref.doc
    path = _ask_project_path(ws.win, doc)
    if path is None:
        return False
    return save(ws, path=path)


def save_as_project(ws) -> bool:
    """The first Save of a tab that is not a project yet: its file becomes a
    new ``.ovt`` project, which the tab then shows.

    The project's root is the file as it is on disk (with its own change log
    migrated into versions, and a ``.dim`` / ``.sgo`` / ``.frf`` kept byte for
    byte as the original); the tab's content, checksums corrected as by any
    save, becomes the newest version on top. A read from a module that was
    never saved starts from what was read."""
    if ws.primary is None:
        return False
    win = ws.win
    from . import container as _cont
    from .project import importers as _imp
    name = os.path.splitext(ws.display_name().replace(" (unsaved)", ""))[0] \
        or "Project"
    path = _ask_path(win, name, None)
    if path is None:
        return False
    info = ask_new_info(win, os.path.splitext(os.path.basename(path))[0])
    src = ws.source_path()
    pending = [l for l in ws.primary.change_log if not l.startswith("VERSION ")]
    fmt = _cont.format_for_kind(ws.primary.platform)
    tmpdir = tempfile.mkdtemp(prefix="ovt-convert-")
    try:
        work = os.path.join(tmpdir, name + (fmt.ext if fmt else ".bin"))
        ws._sync_eeprom_to_primary()
        ws.primary.save(work)                   # checksums corrected
        proj, s = Project(name), None
        if (src and os.path.exists(src) and ws.primary.platform != "hvac"
                and not src.lower().endswith(".edc15bak")):
            try:
                _p, s = _imp.import_file(src, proj)
            except Exception:
                proj, s = Project(name), None
        if s is not None:
            images, defn = _sb.read_back(proj, s.id, work)
            proj.commit(s.id, None, images, label="save", log=pending)
            if defn:
                proj.set_cache(s.id, s.active.id, defn)
        else:
            with open(work, "rb") as fh:
                data = fh.read()
            side = os.path.splitext(work)[0] + ".log"
            log = None
            if os.path.exists(side):
                with open(side, encoding="utf-8", errors="replace") as fh:
                    log = fh.read()
            s = _imp.import_bytes(proj, data, os.path.basename(work),
                                  sidecar_log=log)
        _apply_info(proj, info)
        proj.save(path)
    except Exception as ex:
        ws._save_failed(path, ex)
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    doc = ProjectDoc(proj, path)
    docs(win).append(doc)
    from .gui_files import lock_project
    lock_project(win, path)
    from .recent import add_recent
    add_recent(doc.path)
    ok = load_into(ws, doc, s.id, s.active.id)
    remember(win, doc)
    show_panel(win)
    if ok:
        ws.statusBar().showMessage(
            "Saved %s as a project -- the file itself is unchanged"
            % os.path.basename(path), 8000)
    return ok


# ------------------------------------------------------------- reads
def _read_prompt_skipped() -> bool:
    """Unattended runs (the test suite): no prompt when there is no project
    to add the read to -- it opens as a new project, as it always did."""
    return os.environ.get("OVT_NO_READ_PROMPT", "").strip() not in ("", "0")


def ask_read(win, title, text, informative="", add_label=None, export=None):
    """The "Read complete" prompt: ``"add"`` (to the project in front, offered
    when ``add_label`` is given), ``"new"`` (a new project), ``"export"`` (the
    read was saved to a file by ``export()``, which returns True when it
    saved) or None (discard the read -- confirmed first: a read is not easily
    repeated)."""
    if add_label is None and _read_prompt_skipped():
        return "new"
    SB = QtWidgets.QMessageBox.StandardButton
    Role = QtWidgets.QMessageBox.ButtonRole
    while True:
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question,
                                    title, text, SB.Cancel, win)
        if informative:
            box.setInformativeText(informative)
        add = (box.addButton(add_label, Role.AcceptRole)
               if add_label is not None else None)
        new = box.addButton("New project" if add is not None
                            else "Open as a new project",
                            Role.AcceptRole if add is None else Role.ActionRole)
        exp = (box.addButton("Export to file...", Role.ActionRole)
               if export is not None else None)
        box.setDefaultButton(add if add is not None else new)
        box.exec()
        hit = box.clickedButton()
        if add is not None and hit is add:
            return "add"
        if hit is new:
            return "new"
        if exp is not None and hit is exp:
            if export():
                return "export"
            continue                    # the save was cancelled: ask again
        if QtWidgets.QMessageBox.question(
                win, title, "Discard this read? It is not saved anywhere.",
                SB.Yes | SB.No, SB.No) == SB.Yes:
            return None


def export_read(win, data, name, eeprom=None, source=None) -> bool:
    """Save a read to a file (an .ovt project, or the raw image) instead of
    opening it. True when it was saved."""
    from . import read_export as _rx
    written = _rx.ask_save_read(win, data, name, eeprom=eeprom, source=source,
                                title="Export the read")
    if written and getattr(win, "statusBar", None) is not None:
        try:
            win.statusBar().showMessage(
                "Read saved to %s" % ", ".join(os.path.basename(w)
                                               for w in written), 8000)
        except Exception:
            pass
    return bool(written)


def open_read(win, data, name, eeprom=None, source=None):
    """A read from a module goes straight into a project, with the read itself
    as the as-read original (the root of its history): into the active tab's
    project when the user says so, else a new, untitled project -- or, when
    the user says so, only to a file (Export to file...). It is in the
    crash-recovery store at once, before anything is saved. Returns the tab,
    or None (nothing opened: exported, discarded or failed)."""
    from .platforms.identify import identify
    from .project import importers as _imp
    cur = win.current
    ref = getattr(cur, "project_ref", None)
    here = ref.doc if ref is not None else None
    choice = ask_read(
        win, "Read complete",
        "Add this read to the project %s?" % here.title if here is not None
        else "The read is complete. Open it as a new project?",
        add_label="Add to %s" % here.title if here is not None else None,
        export=lambda: export_read(win, data, name, eeprom=eeprom,
                                   source=source))
    if choice in (None, "export"):
        return None
    target = here if choice == "add" else None
    try:
        if target is not None:
            proj = target.project
            try:
                plat = identify(bytes(data)).platform
            except Exception:
                plat = "unknown"
            same = next((x for x in proj.sets if x.platform == plat
                         and plat != "unknown"), None)
            s = _imp.import_read(proj, data, name, eeprom=eeprom, source=source,
                                 into_set=same.id if same else None)
            doc = target
        else:
            proj = Project(name)
            s = _imp.import_read(proj, data, name, eeprom=eeprom, source=source)
            doc = ProjectDoc(proj, None)
            docs(win).append(doc)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "Read", "The read could not be "
                                       "opened: %s" % ex)
        return None
    b = s.branches[-1]
    ws = open_branch(win, doc, s.id, b.id)
    if ws is None and not doc.tabs(win):
        _drop_doc(win, doc)
        return None
    try:
        doc.saver.snapshot({}, wait=False)      # protected before any save
    except Exception:
        pass
    show_panel(win)
    if ws is not None:
        from . import gui_files as _gf
        _gf._info_on_open(win, ws)
    return ws


def open_eeprom_read(win, data, name, source=None):
    """An EEPROM read goes into a project too: with an EDC15 project in
    front, as a new branch of its ECU set ("EEPROM read <date>", from the
    version in the tab) whose EEPROM is the read -- the EEPROM editor and
    Write EEPROM then work on it; otherwise as a new project holding the
    EEPROM, opened with the EEPROM editor on it."""
    import time as _t
    from .project import importers as _imp
    label = "EEPROM read %s" % _t.strftime("%Y-%m-%d %H:%M")
    cur = win.current
    ref = getattr(cur, "project_ref", None)
    target = None
    s = None
    if ref is not None:
        doc = ref.doc
        s = ref.set if ref.set.platform == "edc15" else next(
            (x for x in doc.project.sets if x.platform == "edc15"), None)
    choice = ask_read(
        win, "EEPROM read complete",
        "Add this EEPROM read to %s (%s)?" % (doc.title, s.name)
        if s is not None else
        "The EEPROM read is complete. Open it as a new project?",
        informative="It becomes a new branch, \"%s\", with the flash of the "
        "version in front and this EEPROM." % label if s is not None else "",
        add_label="Add to %s" % doc.title if s is not None else None,
        export=lambda: export_read(win, data, name, source=source))
    if choice in (None, "export"):
        return None
    if choice == "add":
        target = (doc, s)
    if target is not None:
        doc, s = target
        proj = doc.project
        try:
            base = ref.branch if ref.set is s else s.active
            b = proj.fork(s.id, label, at=base.head)
            if s.member("eeprom") is None:
                _imp._ensure_members(proj, s, [_imp._member(
                    "eeprom", "eeprom", s.platform)])
            proj.commit(s.id, b.id, {"eeprom": bytes(data)}, kind="read",
                        label=label, source=dict(source or {}))
        except Exception as ex:
            QtWidgets.QMessageBox.critical(win, "EEPROM read",
                                           "The read could not be added: %s" % ex)
            return None
        ws = open_branch(win, doc, s.id, b.id)
        refresh_panel(win)
        from . import gui_eeprom
        if ws is not None and gui_eeprom.layouts_for(bytes(data)):
            ws.open_eeprom_editor()     # on the branch's EEPROM, as below
        return ws
    # no EDC15 project in front: a project of its own
    proj = Project(name)
    try:
        s = _imp.import_read(proj, data, name, source=source)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "EEPROM read",
                                       "The read could not be opened: %s" % ex)
        return None
    doc = ProjectDoc(proj, None)
    docs(win).append(doc)
    ws = open_branch(win, doc, s.id, s.branches[-1].id)
    if ws is None:
        _drop_doc(win, doc)
        return None
    try:
        doc.saver.snapshot({}, wait=False)
    except Exception:
        pass
    show_panel(win)
    from . import gui_eeprom
    if gui_eeprom.layouts_for(bytes(data)):
        def _adopt(edited, _ws=ws):
            if bytes(edited) != bytes(_ws.primary.binfile.data):
                _ws.primary.edit_bytes(0, bytes(edited))
                _ws.win._workspace_changed(_ws)
        dlg = gui_eeprom.open_editor(ws, bytes(data),
                                     name="%s [EEPROM]" % doc.title,
                                     on_save=_adopt)
        if dlg is not None:
            dlg.exec()
            _adopt(dlg.data)
    return ws


def keep_attachment(win, filename, data, what) -> bool:
    """Offer to keep ``data`` with the project in front, as an attachment.
    True when it was kept."""
    ref = getattr(win.current, "project_ref", None)
    if ref is None:
        return False
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, "Keep %s" % what,
        "Keep %s with the project %s (Project panel > Attachments)?"
        % (what, ref.doc.title), SB.NoButton, win)
    keep = box.addButton("Keep in the project",
                         QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Save as a file...", QtWidgets.QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.exec()
    if box.clickedButton() is not keep:
        return False
    ref.doc.project.add_attachment(filename, bytes(data), set_id=ref.sid)
    refresh_panel(win)
    try:
        autosave(win, ref.doc)
    except Exception:
        pass
    return True


# ------------------------------------------------------------ flashing
def before_flash_write(win, ws) -> bool:
    """A write is about to send tab ``ws``: with unsaved edits in a project
    tab, offer to save first so what goes into the car is a version in the
    history. False = cancel the write."""
    if ws is None or getattr(ws, "project_ref", None) is None or \
            not ws.is_dirty():
        return True
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, "Write",
        "%s has unsaved changes." % tab_label(ws, ws.project_ref.set.name),
        SB.Cancel, win)
    box.setInformativeText(
        "Save it first, so the version written is in the project's history "
        "(and marked as flashed)? Writing without saving sends the changes as "
        "they are on screen.")
    save_b = box.addButton("Save, then write",
                           QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    raw_b = box.addButton("Write without saving",
                          QtWidgets.QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(save_b)
    box.exec()
    if box.clickedButton() is save_b:
        return save(ws, tab_only=True)
    return box.clickedButton() is raw_b


def mark_flashed(win, ws, what: str = "") -> bool:
    """Tab ``ws`` was written to a control unit: mark its version in the
    project as the last flashed one (Project panel: ⚡)."""
    ref = getattr(ws, "project_ref", None)
    if ref is None:
        return False
    import hashlib
    dirty = ws.is_dirty()
    try:
        sha = hashlib.sha1(bytes(ws.primary.binfile.data)).hexdigest()
    except Exception:
        sha = ""
    ref.doc.project.mark_flashed(ref.sid, ref.bid,
                                 None if dirty else ref.branch.head,
                                 unsaved=dirty, tool=what, sha1=sha)
    for t in ref.doc.tabs(win):
        win._workspace_changed(t)
    refresh_panel(win)
    try:
        autosave(win, ref.doc)
    except Exception:
        pass
    return True


def flashed_text(project, sid) -> str:
    """"Last flashed 2026-10-11 17:50 (Stage 1, saved version)" or ""."""
    rec = project.last_flashed(sid)
    if not rec:
        return ""
    s = project.set(sid)
    b = s.branch(rec.get("branch") or "") if rec.get("branch") else None
    what = "unsaved edits" if rec.get("unsaved") else "saved version"
    return "Last flashed %s (%s%s)" % ((rec.get("ts") or "").replace("T", " ")[:16],
                                       ("%s, " % b.name) if b else "", what)


# ------------------------------------------------- WinOLS and definitions
def definition_kind(path) -> Optional[str]:
    """``"ols"`` for a WinOLS project, ``"xdf"`` / ``"a2l"`` / ``"kp"`` /
    ``"dam"`` for a definition, None for anything else -- from the file's
    content."""
    from .definitions import detect
    from .definitions import winols as _wo
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
        if _wo.is_winols(head):
            with open(path, "rb") as fh:
                return detect(fh.read())
        return detect(head)
    except (OSError, ValueError):
        return None


def _open_new_doc(win, proj, ws=None):
    doc = ProjectDoc(proj, None)
    docs(win).append(doc)
    first = None
    for s in proj.sets:
        t = open_branch(win, doc, s.id, s.active.id,
                        ws=ws if first is None else None)
        first = first or t
    if first is None and not doc.tabs(win):
        _drop_doc(win, doc)
        return None
    try:
        doc.saver.snapshot({}, wait=False)
    except Exception:
        pass
    show_panel(win)
    return first


def open_ols(win, path, ws=None) -> bool:
    """A WinOLS project opens as a new (unsaved) project: its binary, its
    maps as the binary's definition, and the vehicle details entered in
    WinOLS as the project information."""
    from .project import importers as _imp
    name = os.path.splitext(os.path.basename(path))[0]
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        proj = Project(name)
        with open(path, "rb") as fh:
            _imp.import_bytes(proj, fh.read(), os.path.basename(path))
    except Exception as ex:
        QtWidgets.QApplication.restoreOverrideCursor()
        QtWidgets.QMessageBox.critical(
            win, "WinOLS project", "%s could not be imported: %s"
            % (os.path.basename(path), ex))
        return False
    QtWidgets.QApplication.restoreOverrideCursor()
    if ws is not None and not ws.is_empty():
        ws = None
    t = _open_new_doc(win, proj, ws=ws)
    if t is not None:
        msg = "Imported %s as a new project" % os.path.basename(path)
        if proj.report.warnings:
            msg += " -- " + proj.report.warnings[0]
        t.statusBar().showMessage(msg, 10000)
    return t is not None


def open_definition(win, path, kind) -> bool:
    """A definition opened like a file: it holds no binary, so ask which
    binary it describes -- or attach it to the project tab in front."""
    cur = win.current
    ref = getattr(cur, "project_ref", None)
    SB = QtWidgets.QMessageBox.StandardButton
    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Icon.Question, "Open definition",
        "%s is a %s definition: it describes the parameters of a binary but "
        "holds none." % (os.path.basename(path), kind.upper()), SB.Cancel, win)
    choose = box.addButton("Choose the binary...",
                           QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    attach = None
    if ref is not None:
        attach = box.addButton("Attach to %s" % tab_label(cur, ref.set.name),
                               QtWidgets.QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(attach or choose)
    box.exec()
    clicked = box.clickedButton()
    if attach is not None and clicked is attach:
        return attach_definition(win, ref.doc, ref.sid, path)
    if clicked is not choose:
        return False
    from .recent import get_last_dir, set_last_dir
    binp, _f = QtWidgets.QFileDialog.getOpenFileName(
        win, "The binary %s describes" % os.path.basename(path),
        os.path.dirname(path) or get_last_dir(),
        "Binaries (*.bin *.ori *.hex *.mpc);;All files (*)")
    if not binp:
        return False
    set_last_dir(binp)
    from .project import importers as _imp
    try:
        proj, _s = _imp.import_file(binp, definition_path=path)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "Open definition", str(ex))
        return False
    return _open_new_doc(win, proj) is not None


_FILES_FILTER = ("Binaries and containers (*.bin *.ori *.sgo *.frf *.dim *.ols "
                 "*.edc16 *.edc15 *.6hp *.8hp *.vdo);;All files (*)")


def _import_paths(win, proj, paths) -> list:
    """Import each file into ``proj``; returns the new sets. A definition
    beside a binary (same name, .xdf / .a2l / .kp / .dam) comes with it."""
    from .project import importers as _imp
    sets, problems = [], []
    for path in paths:
        try:
            _p, s = _imp.import_file(path, proj)
            sets.append(s)
        except Exception as ex:
            problems.append("%s: %s" % (os.path.basename(path), ex))
    if problems:
        QtWidgets.QMessageBox.warning(win, "Add files", "\n".join(problems))
    return sets


def new_project(win) -> bool:
    """File > New project...: describe the vehicle, then pick the files it
    starts with (an ECU read, the gearbox's .frf, a WinOLS project ...)."""
    values = ask_new_info(win, "")
    from .recent import get_last_dir, set_last_dir
    paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
        win, "Files for the new project", get_last_dir(), _FILES_FILTER)
    if not paths:
        return False
    set_last_dir(paths[0])
    name = (values[0] if values and values[0] else
            os.path.splitext(os.path.basename(paths[0]))[0])
    proj = Project(name)
    _apply_info(proj, values)
    if not _import_paths(win, proj, paths):
        return False
    t = _open_new_doc(win, proj)
    if t is not None:
        doc = t.project_ref.doc
        doc.info_asked = True
    return t is not None


def add_files(win, doc) -> bool:
    """Add files to an open project, each as a set of its own (or a branch
    of the set in front, when the user says so)."""
    from .recent import get_last_dir, set_last_dir
    paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
        win, "Add files to %s" % doc.title, get_last_dir(), _FILES_FILTER)
    if not paths:
        return False
    set_last_dir(paths[0])
    sets = _import_paths(win, doc.project, paths)
    for s in sets:
        open_branch(win, doc, s.id, s.active.id)
    refresh_panel(win)
    return bool(sets)


def attach_definition(win, doc, sid, path) -> bool:
    """Package the definition at ``path`` with set ``sid``'s binary; its
    tabs reopen with it."""
    from .project import importers as _imp
    with open(path, "rb") as fh:
        data = fh.read()
    kind = _imp.definition_type(data)
    if kind is None:
        QtWidgets.QMessageBox.warning(
            win, "Attach a definition", "%s is not a definition this version "
            "reads (XDF, A2L, KP, OLS or DAM)." % os.path.basename(path))
        return False
    main = _sb._main_member(doc.project, sid)
    if main is None:
        return False
    instead = False
    tab = next((t for t in doc.tabs(win) if t.project_ref.sid == sid
                and t.primary is not None), None)
    if tab is not None and getattr(tab.primary, "support", "") == "full":
        # a module already defines this binary: whose maps to show?
        box = QtWidgets.QMessageBox(win)
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setWindowTitle("Attach a definition")
        box.setText("%s is already defined by its module (%s)."
                    % (doc.project.set(sid).name,
                       getattr(tab.primary, "family", "") or "built in"))
        box.setInformativeText(
            "Show the maps of %s beside the module's (in a group of their "
            "own), or instead of them? Instead: the module still corrects "
            "the checksums, but its tuning tools that depend on its own map "
            "list are not available. Remove the definition (Project panel) "
            "to go back." % os.path.basename(path))
        keep = box.addButton("Show them beside the module's",
                             QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        use = box.addButton("Use them instead",
                            QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() not in (use, keep):
            return False
        instead = box.clickedButton() is use
    doc.project.add_definition(sid, main.id, os.path.basename(path), data, kind,
                               instead_of_module=instead)
    reload_set(win, doc, sid)
    refresh_panel(win)
    return True


def reload_set(win, doc, sid):
    """Reopen the set's tabs so a new definition takes effect (a tab with
    unsaved edits is left as it is, and says so)."""
    for t in doc.tabs(win):
        if t.project_ref.sid != sid:
            continue
        if t.is_dirty():
            t.statusBar().showMessage(
                "Save this tab, then reopen it to use the new definition.",
                10000)
            continue
        reload_tab(t)


# ------------------------------------------------------------ closing
def is_last_tab(ws) -> bool:
    doc = doc_of(ws)
    return doc is not None and len(doc.tabs(ws.win)) <= 1


def confirm_close_last(ws) -> bool:
    """Closing a project's last tab closes the project, as File > Close
    project does: ONE question about everything unsaved in it -- the tab's
    edits and the project's own changes (a new branch, a log, a merge) --
    and Save writes the whole project. True = go ahead."""
    doc = doc_of(ws)
    win = ws.win
    if doc is None or len(doc.tabs(win)) > 1 or not doc.is_dirty(win):
        return True
    win.show_workspace(ws)
    SB = QtWidgets.QMessageBox.StandardButton
    r = QtWidgets.QMessageBox.question(
        ws, "Close project", "%s has unsaved changes. Save the project before "
        "closing it?" % doc.title, SB.Save | SB.Discard | SB.Cancel, SB.Save)
    if r == SB.Cancel:
        return False
    if r == SB.Save:
        return save(ws)
    remember(win, doc)
    doc.__dict__["_closing_all"] = True     # the whole project, as one entry
    return True


def tab_removed(win, ws):
    """A project tab went (closed, or another file opened in it): the
    project closes with its last tab."""
    doc = doc_of(ws)
    if doc is None:
        return
    ws.project_ref = None
    released(win, doc)


def released(win, doc):
    """``doc`` lost a tab (a project being imported into keeps open while
    its last tab is replaced: ``doc._hold``)."""
    if doc.tabs(win):
        if not doc.__dict__.get("_closing_all"):
            # (a whole project closing keeps the list it had: each tab
            # going would shorten it to the ones not yet closed)
            remember(win, doc)
    elif not doc.__dict__.get("_hold"):
        _drop_doc(win, doc)
    refresh_panel(win)


def keep_definition(ws) -> bool:
    """Store the tab's resolved definition (ovtsuite.defn) as its branch's
    cache when the project does not hold an up-to-date one, so the next
    open skips identification and matching. Only for a tab without unsaved
    edits -- the snapshot describes the saved bytes. True when it changed
    the cache (in memory; the .ovt is written by a save, or on closing)."""
    ref = getattr(ws, "project_ref", None)
    s = getattr(ws, "primary", None)
    if ref is None or s is None or ws.is_dirty() \
            or getattr(s, "support", "") != "full":
        return False
    if getattr(s, "defn_state", "") == "used":
        return False                    # it came from the stored one
    try:
        from . import defn as _defn
        blob = _defn.build(s, bytes(s.binfile.data))
        proj = ref.doc.project
        if not blob or blob == proj.cache(ref.sid, ref.bid):
            return False
        proj.set_cache(ref.sid, ref.bid, blob)
    except Exception:
        return False
    s.defn_state = "used"               # stored now: not built again here
    ref.doc.__dict__["_cache_dirty"] = True
    return True


def write_definitions(win, doc) -> bool:
    """Write definitions stored since the last save into the .ovt -- only
    when the project has nothing else unsaved, so no change the user did
    not save goes in with them. True when the file was written."""
    for t in doc.tabs(win):
        keep_definition(t)
    if not doc.__dict__.get("_cache_dirty") or not doc.path \
            or doc.project.modified_since_save:
        return False
    try:
        # refused (DiskChanged) when another program changed the file: the
        # definitions are only a cache, nothing is asked -- they are skipped
        doc.project.save(doc.path)
    except Exception:
        return False
    doc.__dict__["_cache_dirty"] = False
    return True


def note_closing(win, ws, entry):
    """A project tab closes: keep what it takes to bring it back, for the
    project's entry in File > Restore project from history -- when it is the
    project's last tab, or the whole project is closing."""
    keep_definition(ws)
    doc = doc_of(ws)
    if doc is None or entry is None:
        return
    if len(doc.tabs(win)) <= 1 or doc.__dict__.get("_closing_all"):
        entry = dict(entry, **session_project(ws))
        doc.__dict__.setdefault("_closing", []).append(entry)


def _push_history(win, doc):
    tabs = doc.__dict__.pop("_closing", [])
    if not doc.path or not tabs or not hasattr(win, "push_closed"):
        return
    import datetime
    win.push_closed({
        "kind": "project", "primary": doc.path, "label": doc.title,
        "vehicle": doc.project.vehicle(), "tabs": tabs,
        "platform": tabs[0].get("platform"),
        "closed": datetime.datetime.now().isoformat(timespec="minutes")})


def _drop_doc(win, doc):
    write_definitions(win, doc)
    _push_history(win, doc)
    if doc in docs(win):
        docs(win).remove(doc)
    from .gui_files import unlock_project
    unlock_project(win, doc.path)
    doc.close()


def close_project(win, doc) -> bool:
    """Close every tab of ``doc`` (asking about unsaved changes once)."""
    tabs = doc.tabs(win)
    if doc.is_dirty(win):
        SB = QtWidgets.QMessageBox.StandardButton
        r = QtWidgets.QMessageBox.question(
            win, "Close project", "%s has unsaved changes. Save before "
            "closing?" % doc.title, SB.Save | SB.Discard | SB.Cancel, SB.Save)
        if r == SB.Cancel:
            return False
        if r == SB.Save and tabs and not save(tabs[0]):
            return False
    remember(win, doc)
    doc.__dict__["_closing_all"] = True     # the whole project, as one entry
    for t in tabs:
        win._remove_workspace(t)
    if doc in docs(win):
        _drop_doc(win, doc)
    refresh_panel(win)
    return True


class DocEntry:
    """A project with unsaved changes of its own and no edited tab, for the
    window's "Unsaved changes" list on exit."""

    def __init__(self, win, doc):
        self.win, self.doc = win, doc

    def tint(self):
        return None

    def tab_label(self):
        return "%s (project: branches, logs, attachments)" % self.doc.title

    def tab_tooltip(self):
        return self.doc.path or "not saved yet"

    def is_dirty(self):
        return self.doc.project.modified_since_save

    def save_checked(self, tab_only=False):
        tabs = self.doc.tabs(self.win)
        if tabs:
            return save(tabs[0], tab_only=True)
        try:
            from . import gui_disksync as _dsg
            return _dsg.write(self.win, self.doc)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self.win, "Save failed", str(ex))
            return False


def unsaved_entries(win) -> list:
    out = []
    for d in docs(win):
        if d.project.modified_since_save and not any(
                t.is_dirty() for t in d.tabs(win)):
            out.append(DocEntry(win, d))
    return out


def shutdown(win):
    """A proper exit: every project's recovery data goes (saved or not --
    the user has been asked), and the working files with it."""
    timer = win.__dict__.get("_ovt_disksync_timer")
    if timer is not None:
        timer.stop()
    for d in list(docs(win)):
        remember(win, d)
        write_definitions(win, d)
        d.close()
    docs(win).clear()
    from .gui_files import unlock_all
    unlock_all(win)


# --------------------------------------------------------- crash recovery
def autosave(win, doc=None) -> None:
    """Snapshot unsaved work of every open project (or one), in the
    background, without any checksum correction."""
    for d in ([doc] if doc is not None else list(docs(win))):
        tabs = d.tabs(win)
        token = tuple((t.project_ref.sid, t.project_ref.bid,
                       t.primary.revision if t.primary else 0,
                       bool(getattr(t, "_eeprom_dirty", False)), t.is_dirty())
                      for t in tabs)

        def working(d=d, tabs=tabs):
            out = {}
            for t in tabs:
                if t.is_dirty() and t.primary is not None:
                    out[(t.project_ref.sid, t.project_ref.bid)] = \
                        _sb.working_images(d.project, t.project_ref.sid,
                                           t.primary, getattr(t, "_eeprom", None)
                                           if getattr(t, "_eeprom_dirty", False)
                                           else None)
            return out
        try:
            d.saver.maybe_snapshot(working, token)
        except Exception:
            pass                        # recovery is best effort, never fatal


def install_disk_watch(win):
    """Notice open projects' files changed by other programs
    (:mod:`ovtsuite.gui_disksync`)."""
    from . import gui_disksync as _dsg
    return _dsg.install(win)


def install_autosave(win):
    t = QtCore.QTimer(win)
    from . import options as _opt
    t.setInterval(_opt.get("projects/autosave_s") * 1000)   # AUTOSAVE_MS
    t.timeout.connect(lambda: autosave(win))
    t.start()
    win.__dict__["_ovt_autosave_timer"] = t
    return t


def offer_recovery(win) -> int:
    """Startup: projects with unsaved work left by a crash. Returns how many
    were recovered."""
    return recover_chosen(win, ask_recovery(win))


def ask_recovery(parent=None, place=None) -> list:
    """Startup, before the main window exists: offer the unsaved work a
    crash left behind. Unticked entries are discarded at once; returns the
    ones to recover (:func:`recover_chosen`), or [] -- also for "Decide
    later", which keeps everything for the next start. ``place(dialog)``
    positions the dialog (the start-up screen)."""
    try:
        pending = _as.pending()
    except Exception:
        return []
    if not pending:
        return []
    dlg = RecoveryDialog(pending, parent)
    if place is not None:
        place(dlg)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return []
    for rec in dlg.discarded():
        _as.discard(rec)
    return list(dlg.recovered())


def recover_chosen(win, recs) -> int:
    """Open the recovered work :func:`ask_recovery` returned. How many."""
    n = 0
    for rec in recs or []:
        if recover(win, rec):
            n += 1
    return n


def recover(win, rec) -> bool:
    try:
        proj, working, warnings = _as.recover(rec)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(win, "Recover", str(ex))
        return False
    doc = ProjectDoc(proj, rec.project_path)
    if doc.path and not os.path.exists(doc.path):
        doc.path = None
    from .gui_files import lock_project
    if doc.path and not lock_project(win, doc.path):
        doc.path = None                 # in another window: saved anew
    docs(win).append(doc)
    ok = True
    targets = list(working) or [(s.id, s.active.id) for s in proj.sets]
    for (sid, bid) in targets:
        ws = open_branch(win, doc, sid, bid)
        if ws is None:
            ok = False
            continue
        images = working.get((sid, bid))
        if images and ws.primary is not None:
            _sb.apply_working(proj, sid, ws.primary, images)
            ws.model.refresh()
            win._workspace_changed(ws)
    if warnings:
        QtWidgets.QMessageBox.warning(win, "Recover", "\n".join(warnings))
    if ok:
        _as.discard(rec)                # the new session keeps its own entry
        autosave(win, doc)
    win.statusBar().showMessage(
        "Recovered %s -- the edits are unsaved until you save." % doc.title, 10000)
    return ok


class RecoveryDialog(QtWidgets.QDialog):
    """Unsaved work left by a crash: recover (ticked) or discard each."""

    def __init__(self, pending, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recover unsaved work")
        self.pending = pending
        v = QtWidgets.QVBoxLayout(self)
        lead = QtWidgets.QLabel(
            "%s did not close properly. This unsaved work can be recovered; "
            "it opens as unsaved changes, so nothing is written until you "
            "save. Unticked entries are discarded." % branding.APP_NAME)
        lead.setWordWrap(True)
        v.addWidget(lead)
        self.list = QtWidgets.QListWidget()
        for rec in pending:
            it = QtWidgets.QListWidgetItem(rec.describe())
            it.setToolTip(rec.project_path or "never saved")
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Checked)
            self.list.addItem(it)
        v.addWidget(self.list)
        bb = QtWidgets.QDialogButtonBox()
        go = bb.addButton("Recover selected",
                          QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        later = bb.addButton("Decide later",
                             QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        later.setToolTip("Keep it all for the next start")
        go.setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(620, 160 + 24 * min(len(pending), 10))

    def _by_state(self, state):
        return [self.pending[i] for i in range(self.list.count())
                if self.list.item(i).checkState() == state]

    def recovered(self):
        return self._by_state(QtCore.Qt.CheckState.Checked)

    def discarded(self):
        return self._by_state(QtCore.Qt.CheckState.Unchecked)


# ------------------------------------------------------- version history
def fill_version_menu(win, menu, ws) -> None:
    """Compare with saved version ▸ for a project tab: the branch's
    checkpoints, newest first."""
    ref = ws.project_ref
    s = ref.set
    chain = s.lineage(ref.branch.head)
    shown = ws.cmp_version["id"] if ws.cmp_version else None
    from . import options as _opt
    for c in chain[:_opt.get("history/version_menu_max")]:
        a = menu.addAction(_ck_text(c))
        if c.id == shown:
            a.setCheckable(True)
            a.setChecked(True)
        a.triggered.connect(
            lambda _c=False, cid=c.id: compare_with_checkpoint(win.current, cid))
    menu.addSeparator()
    menu.addAction("Project panel...", lambda: show_panel(win, focus=True))


def _ck_text(c) -> str:
    when = (c.ts or "").replace("T", " ")[:16]
    kind = {"read": "read", "import": "imported", "revert": "revert",
            "patch": "patch"}.get(c.kind, "")
    label = c.label or c.kind
    if c.parent is None:
        label += "   (original)"
    return "%s   %s%s" % (when, label, "   [%s]" % kind if kind and kind not in
                          label.lower() else "")


def reload_tab(ws) -> bool:
    """Show the branch's (new) head in ``ws`` -- after a revert."""
    ref = ws.project_ref
    keep = ws.comparison is not None and ws.cmp_version is not None
    cid = ws.cmp_version["id"] if keep else None
    ok = load_into(ws, ref.doc, ref.sid, ref.bid)
    if ok and cid and ref.set.checkpoint(cid) is not None:
        compare_with_checkpoint(ws, cid, quiet=True)
    return ok


# ------------------------------------------------------------- exports
def export_member(ws) -> None:
    """Export ▸ Raw binary: one member of the tab's set, as last saved."""
    ref = ws.project_ref
    if not _saved_first(ws):
        return
    s = ref.set
    members = [m for m in s.members
               if ref.doc.project.member_bytes(s.id, m.id, branch=ref.bid) is not None]
    if not members:
        return
    m = members[0]
    if len(members) > 1:
        names = [x.label for x in members]
        pick, ok = QtWidgets.QInputDialog.getItem(
            ws, "Export raw binary", "Which part of %s?" % s.name, names, 0, False)
        if not ok:
            return
        m = members[names.index(pick)]
    _write_export(ws, "%s %s %s.bin" % (ref.doc.title, s.name, m.label),
                  "Binary files (*.bin)",
                  _ex.export_raw(ref.doc.project, s.id, m.id, branch=ref.bid))


def export_raw(ws) -> None:
    """Export ▸ Raw binary. A project tab: one part of its set as last saved.
    Any other tab: its image as it is on screen, with the checksums
    corrected in the copy (the tab itself is left exactly as it is)."""
    if getattr(ws, "project_ref", None) is not None:
        return export_member(ws)
    s = ws.primary
    if s is None:
        return
    data = bytes(s.binfile.data)
    note = ""
    try:
        cs = s.checksum_provider()
        if cs is not None and cs.is_supported(data):
            data = bytes(cs.correct(data, **s._checksum_opts()))
        else:
            note = " (checksums left as they are: none can be corrected here)"
    except Exception as ex:                     # a raw copy is still useful
        note = " (checksums NOT corrected: %s)" % ex
    base = os.path.splitext(ws.display_name().replace(" (unsaved)", ""))[0]
    if _write_export(ws, base + ".bin", "Binary files (*.bin)", data) and note:
        ws.statusBar().showMessage("Exported%s." % note, 8000)


def export_original(ws) -> None:
    ref = ws.project_ref
    if not _saved_first(ws):
        return
    s = ref.set
    origs = [o for o in s.originals]
    if not origs:
        return
    o = origs[0]
    if len(origs) > 1:
        names = ["%s (%s)" % (x.filename, x.format) for x in origs]
        pick, ok = QtWidgets.QInputDialog.getItem(
            ws, "Export original", "Which original?", names, 0, False)
        if not ok:
            return
        o = origs[names.index(pick)]
    try:
        data = _ex.export_original(ref.doc.project, s.id, o.id, branch=ref.bid)
    except _ex.ExportRefused as ex:
        QtWidgets.QMessageBox.information(ws, "Export original", str(ex))
        return
    ext = os.path.splitext(o.filename)[1] or "." + o.format
    _write_export(ws, os.path.splitext(o.filename)[0] + " (%s)%s" % (
        ref.branch.name, ext), "%s files (*%s)" % (o.format.upper(), ext), data)


def _saved_first(ws) -> bool:
    """Exports are of the saved branch: offer to save the tab first."""
    if not ws.is_dirty():
        return True
    SB = QtWidgets.QMessageBox.StandardButton
    r = QtWidgets.QMessageBox.question(
        ws, "Export", "Exports are made from the last saved version. Save "
        "this tab first, so its current edits are included?",
        SB.Save | SB.Ignore | SB.Cancel, SB.Save)
    if r == SB.Cancel:
        return False
    if r == SB.Save:
        return save(ws, tab_only=True)
    return True


def _write_export(ws, name, filt, data):
    from .recent import get_last_dir, set_last_dir
    path, _f = QtWidgets.QFileDialog.getSaveFileName(
        ws, "Export", os.path.join(get_last_dir(), name), filt + ";;All files (*)")
    if not path:
        return False
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as ex:
        QtWidgets.QMessageBox.critical(ws, "Export", str(ex))
        return False
    set_last_dir(path)
    ws.statusBar().showMessage("Exported %s" % os.path.basename(path), 6000)
    return True


# ---------------------------------------------------- project information
#: QSettings key: ask for the project information when a project is created
ASK_INFO_KEY = "projects/ask_info"


class ProjectInfoDialog(QtWidgets.QDialog):
    """What a project is about: its name, and the vehicle and control units
    it belongs to (make, model, chassis code, model year, VIN ...). Every
    field is optional. Shown from the Project panel and File > Project
    information..., and when a project is created (``new``), where it can
    be skipped -- and turned off."""

    def __init__(self, name: str, info: dict, parent=None, new: bool = False):
        from .project.model import INFO_FIELDS, INFO_LABELS
        super().__init__(parent)
        self.setWindowTitle("New project" if new else "Project information")
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self)
        if new:
            hint = QtWidgets.QLabel(
                "Describe the vehicle this project is for. Everything is "
                "optional; you can change it later with <b>File &gt; Project "
                "information...</b>")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name = QtWidgets.QLineEdit(name)
        self.name.setPlaceholderText("Untitled project")
        form.addRow("Project name:", self.name)
        self.fields = {}
        for key, label, hint in INFO_FIELDS:
            if key == "notes":
                continue
            ed = QtWidgets.QLineEdit(info.get(key, ""))
            if hint:
                ed.setPlaceholderText("e.g. " + hint if key != "vin" else hint)
            self.fields[key] = ed
            form.addRow(label + ":", ed)
        # keys this version does not know (a later version's, or an import's)
        for key, value in info.items():
            if key not in INFO_LABELS:
                ed = QtWidgets.QLineEdit(value)
                self.fields[key] = ed
                form.addRow(key + ":", ed)
        vin = self.fields["vin"]
        vin.setMaxLength(17)
        vin.textEdited.connect(self._vin_edited)
        self.vin_note = QtWidgets.QLabel("")
        self.vin_note.setStyleSheet("color: #b36b00;")
        self.vin_note.setVisible(False)
        form.addRow("", self.vin_note)
        self.notes = QtWidgets.QPlainTextEdit(info.get("notes", ""))
        self.notes.setPlaceholderText("Anything else: what was done, by whom, "
                                      "what to watch for ...")
        self.notes.setFixedHeight(80)
        form.addRow("Notes:", self.notes)
        lay.addLayout(form)
        self.ask = None
        if new:
            self.ask = QtWidgets.QCheckBox("Ask when a new project is created")
            self.ask.setChecked(True)
            lay.addWidget(self.ask)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        if new:
            bb.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText(
                "Skip")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._vin_edited(vin.text())
        (self.fields["make"] if new and name else self.name).setFocus()

    def _vin_edited(self, text):
        up = text.upper()
        if up != text:
            ed = self.fields["vin"]
            pos = ed.cursorPosition()
            ed.setText(up)
            ed.setCursorPosition(pos)
        bad = sorted(set(c for c in up if c in "IOQ" or not c.isalnum()))
        note = ""
        if up and len(up) != 17:
            note = "A VIN has 17 characters (this one has %d)." % len(up)
        if bad:
            note = (note + " " if note else "") + \
                "A VIN never contains %s." % ", ".join(
                    "'%s'" % c if c != " " else "spaces" for c in bad)
        self.vin_note.setText(note)
        self.vin_note.setVisible(bool(note))

    def values(self):
        """``(name, info)`` as entered."""
        info = {k: ed.text().strip() for k, ed in self.fields.items()}
        info["vin"] = info.get("vin", "").upper()
        info["notes"] = self.notes.toPlainText().strip()
        return self.name.text().strip(), info


def ask_info_enabled() -> bool:
    if os.environ.get("OVT_NO_INFO_PROMPT", "").strip() not in ("", "0"):
        return False                    # unattended runs (the test suite)
    v = _settings().value(ASK_INFO_KEY, True)
    return v not in (False, "false", "0", 0)


def ask_new_info(win, name: str, info: Optional[dict] = None):
    """The information prompt for a project being created: ``(name, info)``,
    or None when skipped (or turned off). Unticking "Ask ..." turns it off;
    File > Project information... still works."""
    if not ask_info_enabled():
        return None
    dlg = ProjectInfoDialog(name, info or {}, win, new=True)
    ok = dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
    if dlg.ask is not None and not dlg.ask.isChecked():
        _settings().setValue(ASK_INFO_KEY, False)
    return dlg.values() if ok else None


def _apply_info(project, values) -> bool:
    if not values:
        return False
    name, info = values
    changed = project.set_info(info, replace=True)
    if name and name != project.name:
        project.name = name
        project._touch()
        changed = True
    return changed


def edit_info(win, doc, new: bool = False) -> bool:
    """Edit a project's information (File > Project information..., the
    Project panel). True when anything changed (the project is then unsaved)."""
    if new:
        values = ask_new_info(win, doc.title if doc.project.name else "",
                              doc.project.info)
    else:
        dlg = ProjectInfoDialog(doc.project.name or doc.title,
                                doc.project.info, win)
        values = dlg.values() if dlg.exec() == \
            QtWidgets.QDialog.DialogCode.Accepted else None
    doc.info_asked = True
    if not _apply_info(doc.project, values):
        return False
    for t in doc.tabs(win):
        win._workspace_changed(t)
    refresh_panel(win)
    return True


def edit_current_info(win) -> bool:
    ref = getattr(win.current, "project_ref", None)
    return edit_info(win, ref.doc) if ref is not None else False


# --------------------------------------------------------- File menu
def fill_open_menu(win, menu) -> None:
    """File > Open from project ▸: every branch of every open project that
    has no tab, to open (or bring forward)."""
    menu.clear()
    any_ = False
    for d in docs(win):
        open_ = {(t.project_ref.sid, t.project_ref.bid) for t in d.tabs(win)}
        for s in d.project.sets:
            for b in s.branches:
                a = menu.addAction("%s ▸ %s · %s%s" % (
                    d.title, s.name, b.name,
                    "   (open)" if (s.id, b.id) in open_ else ""))
                a.triggered.connect(
                    lambda _c=False, d=d, sid=s.id, bid=b.id:
                        open_branch(win, d, sid, bid))
                any_ = True
        menu.addSeparator()
    if not any_:
        a = menu.addAction("(no project open)")
        a.setEnabled(False)


# ------------------------------------------------- logs, patches, files
def _att(doc, kind, aid):
    lst = {"log": doc.project.logs, "patch": doc.project.patches,
           "attachment": doc.project.attachments}[kind]
    a = next(x for x in lst if x.id == aid)
    raw = {"log": doc.project.log_bytes, "patch": doc.project.patch_bytes,
           "attachment": doc.project.attachment_bytes}[kind](aid)
    return a, raw


def export_attachment(parent, doc, kind, aid) -> Optional[str]:
    """A log, patch or attachment kept in the project, written to a file
    (Project panel > Export...). Returns the path written."""
    a, raw = _att(doc, kind, aid)
    from .recent import get_last_dir, set_last_dir
    dkind = {"log": "log", "patch": "patch"}.get(kind)
    filt = {"log": "CSV data log (*.csv);;All files (*)",
            "patch": "Patch files (*.patch);;All files (*)"}.get(
                kind, "All files (*)")
    path, _f = QtWidgets.QFileDialog.getSaveFileName(
        parent, "Export %s" % a.filename,
        os.path.join(get_last_dir(dkind), a.filename), filt)
    if not path:
        return None
    with open(path, "wb") as fh:
        fh.write(raw)
    set_last_dir(path, dkind)
    return path


def kept(doc, kind) -> list:
    """The logs / patches / attachments kept in ``doc``."""
    return {"log": doc.project.logs, "patch": doc.project.patches,
            "attachment": doc.project.attachments}[kind]


def export_kept(win, kind) -> Optional[str]:
    """File > Export > Data log / Patch / Attachment: pick one of the
    project's, write it to a file."""
    ref = getattr(win.current, "project_ref", None)
    if ref is None:
        return None
    items = kept(ref.doc, kind)
    if not items:
        return None
    a = items[0]
    if len(items) > 1:
        names = ["%s   (%s)" % (x.filename, x.added.replace("T", " ")[:16])
                 for x in items]
        pick, ok = QtWidgets.QInputDialog.getItem(
            win, "Export", "Which %s of %s?" % (kind, ref.doc.title), names,
            len(names) - 1, False)
        if not ok:
            return None
        a = items[names.index(pick)]
    return export_attachment(win, ref.doc, kind, a.id)


def keep_log(win, ws, path) -> bool:
    """A finished data log goes into the project of tab ``ws`` (for its
    set). The file at ``path`` is a working copy."""
    ref = getattr(ws, "project_ref", None)
    if ref is None or not path or not os.path.exists(path):
        return False
    with open(path, "rb") as fh:
        data = fh.read()
    ref.doc.project.add_log(os.path.basename(path), data, set_id=ref.sid)
    for t in ref.doc.tabs(win):
        win._workspace_changed(t)
    refresh_panel(win)
    try:
        autosave(win, ref.doc)          # safe before the project is saved
    except Exception:
        pass
    return True


def keep_patch(ws, pt, filename) -> bool:
    """A generated patch kept in tab ``ws``'s project."""
    import json
    ref = getattr(ws, "project_ref", None)
    if ref is None:
        return False
    data = json.dumps(pt.to_dict(), indent=1).encode("utf-8")
    ref.doc.project.add_patch(filename, data, set_id=ref.sid)
    for t in ref.doc.tabs(ws.win):
        ws.win._workspace_changed(t)
    refresh_panel(ws.win)
    return True


def apply_project_patch(ws, doc, pid) -> bool:
    """Apply a patch kept in ``doc`` to tab ``ws`` (Edit > Apply patch, or
    the Project panel)."""
    import json
    from . import patch as _patch
    a, raw = _att(doc, "patch", pid)
    try:
        pt = _patch.Patch.from_dict(json.loads(raw.decode("utf-8")))
    except ValueError as ex:
        QtWidgets.QMessageBox.warning(ws, "Apply Patch",
                                      "Could not read this patch: %s" % ex)
        return False
    from .gui import PatchApplyDialog
    PatchApplyDialog(ws.primary, pt, a.filename, ws).exec()
    ws.model.refresh()
    ws._status()
    return True


# --------------------------------------------------------------- panel
_PROJECT_ICON = None


def project_icon():
    """The .ovt file icon (data/ovt_project.svg; the small drawing at list
    sizes), for projects in lists -- the Project panel, the start page."""
    global _PROJECT_ICON
    if _PROJECT_ICON is None:
        from PySide6 import QtGui
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        icon = QtGui.QIcon()
        for name, sizes in (("ovt_project_small.svg", (16, 24, 32)),
                            ("ovt_project.svg", (48, 64, 128, 256))):
            path = os.path.join(base, name)
            if os.path.exists(path):
                for n in sizes:
                    icon.addFile(path, QtCore.QSize(n, n))
        _PROJECT_ICON = icon
    return _PROJECT_ICON


def show_panel(win, focus=False):
    """Show the Project panel. ``focus`` (the menu): give it the keyboard --
    when it is collapsed, it flies out until the focus leaves it."""
    dock = panel(win)
    first = not dock.isVisible() and not dock.__dict__.get("_shown_once")
    dock.show()
    if first:
        dock.__dict__["_shown_once"] = True
        dock.restore_state()
    dock.refresh()
    if focus:
        dock.show_and_focus(dock.tree)


def toggle_panel(win):
    """View > Project panel (and its toolbar button): hidden or collapsed to
    its strip -> shown expanded, to stay (and given the keyboard); expanded
    -> collapsed to the strip again. Floating -> closed."""
    dock = panel(win)
    if dock.isVisible() and dock.isFloating():
        dock.hide()
        return
    if dock.isVisible() and not dock.is_collapsed():
        dock.collapse()
        return
    show_panel(win)
    dock.expand()                       # (out of a flyout too)
    dock.raise_()
    dock.tree.setFocus()


def refresh_panel(win):
    dock = win.__dict__.get("_ovt_panel")
    if dock is not None and dock.isVisible():
        dock.refresh()


def panel(win) -> "ProjectPanel":
    dock = win.__dict__.get("_ovt_panel")
    if dock is None:
        dock = ProjectPanel(win)
        win.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        dock.hide()
        win.__dict__["_ovt_panel"] = dock
        win.tabs.currentChanged.connect(lambda _i: refresh_panel(win))
    return dock


_ROLE = QtCore.Qt.ItemDataRole.UserRole
#: the Project panel's lists of files kept with a project
_ATT_TITLE = {"logs": "Logs", "patches": "Patches", "attachments": "Attachments"}
_ATT_ITEM = {"logs": "log", "patches": "patch", "attachments": "attachment"}


class ProjectPanel(AutoHideDock):
    """Every open project: its sets and branches (double-click opens one in a
    tab), each branch's history, the logs and the attachments. Right-click
    for what can be done with an item. Resizable, and collapsible to a strip
    that shows the panel when the mouse rests on it (gui_autohide)."""

    settings_key = "projects/panel"

    def __init__(self, win):
        super().__init__("Project", win)
        self.setObjectName("ProjectPanel")
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        # several versions can be picked (Ctrl/Shift-click) to merge them
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._double)
        self.set_body(self.tree)
        self.setMinimumWidth(self.minimum_body_width())

    # -- building -------------------------------------------------------
    def refresh(self):
        from .gui import platform_badge
        expanded = self._expanded()
        self.tree.clear()
        cur = self.win.current
        cur_ref = getattr(cur, "project_ref", None)
        for d in docs(self.win):
            top = QtWidgets.QTreeWidgetItem(
                ["%s%s" % (d.title, "*" if d.is_dirty(self.win) else "")])
            top.setData(0, _ROLE, ("doc", d))
            top.setIcon(0, project_icon())
            tip = [d.path or "not saved yet"]
            tip += ["%s: %s" % (k, v.replace("\n", " / "))
                    for k, v in d.project.info_rows()]
            tip.append("Double-click for the project information")
            top.setToolTip(0, "\n".join(tip))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            open_ = {(t.project_ref.sid, t.project_ref.bid): t
                     for t in d.tabs(self.win)}
            for s in d.project.sets:
                badge, long_name, col = platform_badge(s.platform)
                si = QtWidgets.QTreeWidgetItem(["%s  %s" % (badge, s.name)])
                tip = "%s -- %s" % (long_name, ", ".join(m.label for m in s.members))
                mm = _sb._main_member(d.project, s.id)
                if mm is not None and mm.definition:
                    tip += "\nDefinition: %s (%s)" % (
                        os.path.basename(mm.definition.get("path", "")),
                        str(mm.definition.get("type", "")).upper())
                ft = flashed_text(d.project, s.id)
                if ft:
                    tip += "\n" + ft
                si.setToolTip(0, tip)
                si.setData(0, _ROLE, ("set", d, s.id))
                if col is not None:
                    pm = QtGui.QPixmap(10, 10)
                    pm.fill(col)
                    si.setIcon(0, QtGui.QIcon(pm))
                top.addChild(si)
                bad = set()
                if d.project.report.damaged or d.project.report.missing:
                    bad = set(d.project.unreadable_checkpoints(s.id))
                for b in s.branches:
                    tab = open_.get((s.id, b.id))
                    star = "★ " if s.active is not None and b.id == s.active.id else ""
                    bi = QtWidgets.QTreeWidgetItem(["%s%s%s%s" % (
                        star, b.name, "   (open)" if tab else "",
                        "*" if tab is not None and tab.is_dirty() else "")])
                    bi.setData(0, _ROLE, ("branch", d, s.id, b.id))
                    bi.setToolTip(0, "Double-click to open in a tab")
                    if tab is not None and tab is cur:
                        f = bi.font(0)
                        f.setUnderline(True)
                        bi.setFont(0, f)
                    si.addChild(bi)
                    flashed = d.project.last_flashed(s.id) or {}
                    for c in s.lineage(b.head):
                        text = _ck_text(c)
                        if flashed.get("checkpoint") == c.id:
                            text += "   ⚡ flashed %s" % (
                                flashed.get("ts") or "").replace("T", " ")[:16]
                        ci = QtWidgets.QTreeWidgetItem([text])
                        ci.setData(0, _ROLE, ("ck", d, s.id, b.id, c.id))
                        if c.id in bad:
                            ci.setForeground(0, QtGui.QBrush(QtGui.QColor("red")))
                        bi.addChild(ci)
            for kind, lst in (("logs", d.project.logs),
                              ("patches", d.project.patches),
                              ("attachments", d.project.attachments)):
                li = QtWidgets.QTreeWidgetItem(["%s (%d)%s" % (
                    _ATT_TITLE[kind], len(lst),
                    "  -- keeps %d" % d.project.max_logs
                    if kind == "logs" and d.project.max_logs else "")])
                li.setData(0, _ROLE, (kind, d))
                top.addChild(li)
                for a in lst:
                    ai = QtWidgets.QTreeWidgetItem(["%s%s" % (
                        "📌 " if a.pinned else "", a.filename)])
                    where = ""
                    if a.set:
                        try:
                            where = "\n" + d.project.set(a.set).name
                        except KeyError:
                            pass
                    ai.setToolTip(0, "%s%s%s\nRight-click to export" % (
                        a.added.replace("T", " "), where,
                        ("\n" + a.note) if a.note else ""))
                    ai.setData(0, _ROLE, (_ATT_ITEM[kind], d, a.id))
                    li.addChild(ai)
            top.setExpanded(True)
            for i in range(top.childCount()):
                ch = top.child(i)
                key = self._key(ch)
                if key in expanded or (expanded == set() and
                                       ch.data(0, _ROLE)[0] == "set"):
                    ch.setExpanded(True)
                for j in range(ch.childCount()):
                    if self._key(ch.child(j)) in expanded:
                        ch.child(j).setExpanded(True)
            if cur_ref is not None and cur_ref.doc is d:
                self.tree.setCurrentItem(top)

    @staticmethod
    def _key(item):
        data = item.data(0, _ROLE)
        if not data:
            return None
        return tuple(x if isinstance(x, str) else id(x) for x in data)

    def _expanded(self):
        out = set()

        def walk(it):
            for i in range(it.childCount()):
                ch = it.child(i)
                if ch.isExpanded():
                    out.add(self._key(ch))
                walk(ch)
        walk(self.tree.invisibleRootItem())
        return out

    # -- acting -----------------------------------------------------------
    def _double(self, item, _col=0):
        data = item.data(0, _ROLE)
        if not data:
            return
        if data[0] == "doc":
            edit_info(self.win, data[1])
        elif data[0] == "branch":
            open_branch(self.win, data[1], data[2], data[3])
        elif data[0] == "ck":
            ws = self._tab_for(data[1], data[2], data[3]) or \
                open_branch(self.win, data[1], data[2], data[3])
            if ws is not None:
                compare_with_checkpoint(ws, data[4])
        elif data[0] == "patch":
            self._apply_patch(data)
        elif data[0] == "log":
            self._view_log(data)            # view it; Export... is on the menu
        elif data[0] == "attachment":
            self._export_att(data)

    def _picked_versions(self, item, d, sid):
        """The selected versions of the same set as ``item`` (just ``item``
        when it is not among the selected)."""
        sel = self.tree.selectedItems()
        if item not in sel:
            return [item.data(0, _ROLE)[4]]
        out = []
        for it in sel:
            x = it.data(0, _ROLE)
            if x and x[0] == "ck" and x[1] is d and x[2] == sid and x[4] not in out:
                out.append(x[4])
        return out

    def _merge(self, d, sid, cids):
        s = d.project.set(sid)
        names = [s.checkpoint(c).label for c in cids if s.checkpoint(c)]
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Merge versions",
            "Merge %d versions into one? The newest keeps its place; reverting "
            "to it still gives the same bytes.\n\nName for the merged version:"
            % len(cids), QtWidgets.QLineEdit.EchoMode.Normal,
            "; ".join(n for n in names if n))
        if not ok:
            return
        try:
            merge_versions(self.win, d, sid, cids, name.strip())
        except (ValueError, KeyError) as ex:
            QtWidgets.QMessageBox.information(self, "Merge versions", str(ex))

    def _tab_for(self, d, sid, bid):
        for t in d.tabs(self.win):
            if t.project_ref.sid == sid and t.project_ref.bid == bid:
                return t
        return None

    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, _ROLE)
        if not data:
            return
        kind, d = data[0], data[1]
        m = QtWidgets.QMenu(self)
        if kind == "doc":
            m.addAction("Project information...", lambda: edit_info(self.win, d))
            m.addAction("Add files to the project...", lambda: add_files(self.win, d))
            m.addAction("Save project", lambda: self._save_doc(d))
            m.addAction("Rename project...", lambda: self._rename_doc(d))
            m.addAction("Keep at most ... logs", lambda: self._log_cap(d))
            m.addSeparator()
            m.addAction("Close project", lambda: close_project(self.win, d))
        elif kind == "set":
            sid = data[2]
            m.addAction("Rename...", lambda: self._rename_set(d, sid))
            main = _sb._main_member(d.project, sid)
            if main is not None:
                m.addSeparator()
                m.addAction("Attach a definition (XDF, A2L, KP, OLS, DAM)...",
                            lambda: self._attach_def(d, sid))
                rm = m.addAction("Remove the definition",
                                 lambda: self._remove_def(d, sid))
                rm.setEnabled(bool(main.definition))
        elif kind == "branch":
            sid, bid = data[2], data[3]
            m.addAction("Open in a tab", lambda: open_branch(self.win, d, sid, bid))
            m.addAction("Make it the active branch",
                        lambda: self._do(d, lambda: d.project.set_active(sid, bid)))
            m.addAction("Rename...", lambda: self._rename_branch(d, sid, bid))
            m.addAction("New branch from here...",
                        lambda: self._new_branch(d, sid, d.project.set(sid)
                                                 .branch(bid).head))
            m.addSeparator()
            de = m.addAction("Delete branch...",
                             lambda: self._delete_branch(d, sid, bid))
            de.setEnabled(len(d.project.set(sid).branches) > 1
                          and self._tab_for(d, sid, bid) is None)
            if self._tab_for(d, sid, bid) is not None:
                de.setToolTip("Close its tab first")
        elif kind == "ck":
            sid, bid, cid = data[2], data[3], data[4]
            picked = self._picked_versions(item, d, sid)
            if len(picked) >= 2:
                m.addAction("Merge %d versions into one..." % len(picked),
                            lambda: self._merge(d, sid, picked))
                m.addSeparator()
            m.addAction("Compare with this version",
                        lambda: self._double(item))
            m.addAction("Revert the branch to this version...",
                        lambda: self._revert(d, sid, bid, cid))
            m.addAction("New branch from this version...",
                        lambda: self._new_branch(d, sid, cid))
        elif kind in ("logs", "patches", "attachments"):
            m.addAction("Add %s..." % _ATT_ITEM[kind],
                        lambda: self._add_att(d, kind))
            if kind == "logs":
                m.addAction("Keep at most ... logs", lambda: self._log_cap(d))
        elif kind in ("log", "patch", "attachment"):
            aid = data[2]
            if kind == "patch":
                ap = m.addAction("Apply to this tab...",
                                 lambda: self._apply_patch(data))
                cur = self.win.current
                ap.setEnabled(cur is not None and cur.primary is not None)
            elif kind == "log":
                m.addAction("Open in Data logging", lambda: self._view_log(data))
                from . import gui_ai as _ga
                m.addAction("Summarise with AI...",
                            lambda: _ga.summarise_log(self.win, d, aid))
            m.addAction("Export...", lambda: self._export_att(data))
            if kind == "log":
                a = next(x for x in d.project.logs if x.id == aid)
                m.addAction("Unpin" if a.pinned else "Pin (never auto-deleted)",
                            lambda: self._do(d, lambda: d.project.pin_log(
                                aid, not a.pinned)))
            m.addSeparator()
            m.addAction("Remove", lambda: self._do(d, lambda: (
                d.project.remove_log(aid) if kind == "log"
                else d.project.remove_patch(aid) if kind == "patch"
                else d.project.remove_attachment(aid))))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _do(self, d, fn):
        try:
            fn()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Project", str(ex))
        self._changed(d)

    def _changed(self, d):
        for t in d.tabs(self.win):
            self.win._workspace_changed(t)
        self.refresh()

    def _save_doc(self, d):
        tabs = d.tabs(self.win)
        if tabs:
            save(tabs[0])
        elif d.path:
            from . import gui_disksync as _dsg
            _dsg.write(self.win, d)
        self.refresh()

    def _rename_doc(self, d):
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename project", "Name:", text=d.title)
        if ok and name.strip():
            d.project.name = name.strip()
            d.project._touch()
            self._changed(d)

    def _rename_set(self, d, sid):
        s = d.project.set(sid)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename", "Name:", text=s.name)
        if ok and name.strip():
            self._do(d, lambda: d.project.rename_set(sid, name.strip()))

    def _rename_branch(self, d, sid, bid):
        b = d.project.set(sid).branch(bid)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename branch", "Name:", text=b.name)
        if ok and name.strip():
            self._do(d, lambda: d.project.rename_branch(sid, bid, name.strip()))

    def _new_branch(self, d, sid, cid):
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New branch", "Name of the new branch (e.g. Stage 1, Economy):")
        if not ok or not name.strip():
            return
        try:
            b = d.project.fork(sid, name.strip(), at=cid)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "New branch", str(ex))
            return
        self._changed(d)
        open_branch(self.win, d, sid, b.id)

    def _delete_branch(self, d, sid, bid):
        b = d.project.set(sid).branch(bid)
        if QtWidgets.QMessageBox.question(
                self, "Delete branch",
                "Delete the branch \"%s\"? Its history stays in the project "
                "(and the as-read original always does)." % b.name) \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._do(d, lambda: d.project.delete_branch(sid, bid))

    def _revert(self, d, sid, bid, cid):
        tab = self._tab_for(d, sid, bid)
        if tab is not None and tab.is_dirty():
            QtWidgets.QMessageBox.information(
                self, "Revert", "That branch's tab has unsaved changes. Save or "
                "undo them first.")
            return
        c = d.project.set(sid).checkpoint(cid)
        if QtWidgets.QMessageBox.question(
                self, "Revert", "Make \"%s\" the branch's current version? "
                "This adds a new version to the history; nothing is lost."
                % (c.label or c.kind)) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            d.project.revert(sid, bid, cid)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Revert", str(ex))
            return
        if tab is not None:
            reload_tab(tab)
        self._changed(d)

    def _add_att(self, d, kind):
        from .recent import get_last_dir, set_last_dir
        dkind = {"logs": "log", "patches": "patch"}.get(kind)
        filt = {"logs": "Logs (*.csv *.log *.txt);;All files (*)",
                "patches": "Patch files (*.patch);;All files (*)"}.get(
                    kind, "All files (*)")
        paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add to project", get_last_dir(dkind), filt)
        cur = self.win.current
        sid = cur.project_ref.sid if getattr(cur, "project_ref", None) is not None \
            and cur.project_ref.doc is d else None
        for p in paths:
            with open(p, "rb") as fh:
                data = fh.read()
            if kind == "logs":
                d.project.add_log(os.path.basename(p), data, set_id=sid)
            elif kind == "patches":
                d.project.add_patch(os.path.basename(p), data, set_id=sid)
            else:
                d.project.add_attachment(os.path.basename(p), data, set_id=sid)
            set_last_dir(p, dkind)
        if paths:
            self._changed(d)

    def _export_att(self, data):
        kind, d, aid = data[0], data[1], data[2]
        export_attachment(self, d, kind, aid)

    def _apply_patch(self, data):
        _kind, d, aid = data[0], data[1], data[2]
        cur = self.win.current
        if cur is None or cur.primary is None:
            return
        apply_project_patch(cur, d, aid)

    def _view_log(self, data):
        _kind, d, aid = data[0], data[1], data[2]
        a = next(x for x in d.project.logs if x.id == aid)
        path = os.path.join(d.tmpdir, "logs", a.id, a.filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(d.project.log_bytes(aid))
        self.win.open_datalog()
        dlg = self.win.__dict__.get("_datalog_dlg")
        if dlg is not None and hasattr(dlg, "view_file"):
            dlg.view_file(path)             # in the Log file tab, brought forward
        elif dlg is not None and hasattr(dlg, "_load_view"):
            dlg._load_view(path)

    def _attach_def(self, d, sid):
        from .definitions import FILTER
        from .recent import get_last_dir, set_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Attach a definition", get_last_dir("definition"), FILTER)
        if not path:
            return
        set_last_dir(path, "definition")
        attach_definition(self.win, d, sid, path)
        self._changed(d)

    def _remove_def(self, d, sid):
        main = _sb._main_member(d.project, sid)
        self._do(d, lambda: d.project.remove_definition(sid, main.id))
        self._reload_set(d, sid)

    def _reload_set(self, d, sid):
        reload_set(self.win, d, sid)

    def _log_cap(self, d):
        n, ok = QtWidgets.QInputDialog.getInt(
            self, "Logs kept", "Keep at most this many logs (0 = no limit). The "
            "oldest unpinned logs are deleted first; pinned logs are always "
            "kept.", d.project.max_logs, 0, 10000)
        if ok:
            self._do(d, lambda: setattr(d.project, "max_logs", n))


__all__ = ["open_project", "open_branch", "load_ref", "save", "save_as",
           "save_as_project", "is_project", "parse_ref", "exists", "show_panel",
           "session_groups", "session_project",
           "offer_recovery", "install_autosave", "install_disk_watch",
           "shutdown"]
