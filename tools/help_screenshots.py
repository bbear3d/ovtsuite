# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Regenerate the screenshots in the help pages from the real dialogs.

    python tools/help_screenshots.py              # every shot whose inputs exist
    python tools/help_screenshots.py flash-dialog main-window   # just these
    python tools/help_screenshots.py --list       # what there is, and what it needs
    python tools/help_screenshots.py --check      # every image a page shows has a recipe

Each picture in a help page (``![...](images/<id>.png)``) is made by a
*recipe*: a function in ``tools/help_shots/*.py`` decorated with :func:`shot`
that builds the window in the state to show and returns it. The tool opens it
off screen at a fixed size, renders it, and writes ``images/<id>.png`` into
the help folder the recipe names -- so the pictures follow the interface
instead of drifting from it. Run it after changing a dialog; ``build.ps1
-HelpShots`` runs it before freezing.

Windows are rendered by the platform's own style (not shown on screen), so run
it on the machine whose look the help should have. Recipes that need a
reference file (``ctx.ref("edc16_lq")``, names in ``tools/help_shots/refs.json``)
are skipped, with a note, when the file is not in Reference/.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

PKG_DIR = os.path.join(ROOT, "ovtsuite")
REFS_FILE = os.path.join(ROOT, "tools", "help_shots", "refs.json")


# recipes import this module by name; when it runs as a script, that must be
# this same module, or they would register into a second copy
sys.modules.setdefault("help_screenshots", sys.modules[__name__])


class Skip(Exception):
    """A recipe cannot run here (a reference file or a module is missing)."""


@dataclass
class Shot:
    id: str
    folder: str                       # help folder, relative to ovtsuite/
    make: Callable
    size: Tuple[int, int] = (1100, 700)
    what: str = ""
    #: crop to a child widget (objectName or callable(window) -> widget)
    crop: Optional[object] = None

    @property
    def path(self) -> str:
        return os.path.join(PKG_DIR, *self.folder.split("/"), "images", self.id + ".png")


SHOTS: Dict[str, Shot] = {}

#: the folder the sample projects are saved in, removed at exit
_HELP_TEMP: List[str] = []


def shot(id: str, folder: str = "data/help", size=(1100, 700), what: str = "", crop=None):
    """Register a recipe: ``make(ctx) -> QWidget`` (the window to picture)."""
    def deco(fn):
        if id in SHOTS:
            raise ValueError("two recipes for shot %r" % id)
        SHOTS[id] = Shot(id, folder, fn, tuple(size), what or (fn.__doc__ or "").strip(), crop)
        return fn
    return deco


class Context:
    """What a recipe gets: the application, reference files, and helpers to
    open the main window and files without the modal pop-ups a user sees."""

    def __init__(self, app):
        from PySide6 import QtCore, QtWidgets
        self.app = app
        self.QtCore, self.QtWidgets = QtCore, QtWidgets
        try:
            with open(REFS_FILE, encoding="utf-8") as fh:
                self.refs = json.load(fh)
        except (OSError, ValueError):
            self.refs = {}
        self._keep = []

    # -- inputs --------------------------------------------------------------
    def ref(self, key: str) -> str:
        """Absolute path of reference file ``key`` (refs.json), or Skip."""
        rel = self.refs.get(key)
        if not rel:
            raise Skip("no reference file named %r in refs.json" % key)
        p = os.path.join(ROOT, *rel.split("/"))
        if not os.path.exists(p):
            raise Skip("reference file missing: %s" % rel)
        return p

    def need_module(self, mid: str):
        from ovtsuite.modules import registry
        if not registry.is_installed(mid):
            raise Skip("the %s module is not installed here" % mid)

    # -- building ------------------------------------------------------------
    def settle(self, ms: int = 300):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def keep(self, obj):
        """Keep ``obj`` alive until the shot is taken."""
        self._keep.append(obj)
        return obj

    #: the sample project each reference file is imported into: its name
    #: and vehicle (everything the user works on is a project)
    PROJECTS = {
        "edc16_lq": ("A6 BHW swap", {"make": "Audi", "model": "A6",
                                     "chassis": "C6", "year": "2006"}),
        "edc16_q": ("Passat B6 1.9 TDI", {"make": "VW", "model": "Passat",
                                          "chassis": "B6", "year": "2006"}),
        "edc15_bd": ("Golf IV ALH", {"make": "VW", "model": "Golf",
                                     "chassis": "1J", "year": "2001"}),
        "zf6hp19": ("A6 C6 gearbox", {"make": "Audi", "model": "A6",
                                      "chassis": "C6", "year": "2005"}),
        "zf6hp28_tdi": ("A6 3.0 TDI", {"make": "Audi", "model": "A6",
                                       "chassis": "C6", "year": "2007"}),
        "zf8hp": ("A6 C7 3.0 TDI", {"make": "Audi", "model": "A6",
                                    "chassis": "C7", "year": "2012"}),
        "aisin09d": ("Touareg V6 TDI", {"make": "VW", "model": "Touareg",
                                        "chassis": "7L", "year": "2006"}),
        "vdo_vwk501": ("A4 B6 cluster", {"make": "Audi", "model": "A4",
                                         "chassis": "B6", "year": "2003"}),
        "hvac_4f": ("A6 C6 Climatronic", {"make": "Audi", "model": "A6",
                                          "chassis": "C6", "year": "2006"}),
    }

    def project(self, path: str, name: Optional[str] = None,
                info: Optional[dict] = None, branch: Optional[str] = None,
                setup: Optional[Callable] = None) -> str:
        """``path`` imported into a new project and saved in a temporary
        folder (File > Import, then Save); the ``.ovt``'s path. ``branch``:
        a branch of that name forked from the import, made the active one.
        ``setup(project, set)`` changes the project before it is saved."""
        import tempfile
        from ovtsuite import project as ovt
        key = next((k for k, rel in self.refs.items()
                    if isinstance(rel, str)
                    and os.path.normcase(os.path.join(ROOT, *rel.split("/")))
                    == os.path.normcase(os.path.abspath(path))), None)
        dname, dinfo = self.PROJECTS.get(key, (None, None))
        proj, s = ovt.import_file(path)
        proj.name = name or dname or proj.name
        if info or dinfo:
            proj.set_info(info or dinfo)
        if branch:
            b = proj.fork(s.id, branch)
            proj.set_active(s.id, b.id)
        if setup is not None:
            setup(proj, s)
        # one tidy folder (the path shows in File information); the shots
        # are taken one at a time, so a name is never open twice
        d = os.path.join(tempfile.gettempdir(), "OVT-Suite help", "Projects")
        os.makedirs(d, exist_ok=True)
        if not _HELP_TEMP:
            import atexit
            import shutil
            _HELP_TEMP.append(os.path.dirname(d))
            atexit.register(shutil.rmtree, os.path.dirname(d), True)
        out = os.path.join(d, proj.name + ".ovt")
        proj.save(out)
        return out

    def open_project(self, window, path: str, **kw):
        """Open ``path`` -- an ``.ovt``, or a binary imported into a new
        project first (:meth:`project`) -- in ``window``; its tab."""
        from ovtsuite import gui_project
        ovt = path if path.lower().endswith(".ovt") else self.project(path, **kw)
        if not gui_project.open_project(window, ovt):
            raise Skip("could not open %s" % path)
        self.settle(100)
        return window.current

    def main_window(self, path: Optional[str] = None, comparison: Optional[str] = None,
                    **kw):
        """A main window with ``path`` open as a project tab (see
        :meth:`open_project`; it starts out compared with the original as
        read), and ``comparison`` imported as its comparison instead. The
        Project panel is folded to its strip unless ``panel=True``."""
        from ovtsuite import gui
        keep_panel = kw.pop("panel", False)
        w = self.keep(gui.MainWindow())
        w.clear_closed()
        if path:
            self.open_project(w, path, **kw)
        if comparison:
            w.current._load_comparison(comparison)
        if path and not keep_panel:
            # the Project panel folded to its strip: the pictures are about
            # the file (its own page has a picture of the panel)
            from ovtsuite import gui_project
            gui_project.panel(w).collapse(remember=False)
        return w

    def entity(self, window, name: str):
        """The parameter called ``name`` in the window's current file."""
        ws = window.current
        for e in ws.primary.entities:
            if e.name == name or getattr(e, "title", None) == name:
                return e
        raise Skip("no parameter %r in this file" % name)

    def open_table(self, window, name: str, pin: bool = False):
        e = self.entity(window, name)
        window.current.open_table(e, pin=pin)
        self.settle(200)
        return e

    def reset(self):
        for w in reversed(self._keep):
            try:
                if hasattr(w, "close_discarding"):
                    w.close_discarding()
                else:
                    w.close()
            except Exception:
                pass
        self._keep = []


def _quiet_popups():
    """Answer the pop-ups a user would see, so a recipe never blocks."""
    from PySide6 import QtWidgets
    from ovtsuite import gui
    gui.Workspace.show_file_info = lambda self, *a, **k: None
    for name in ("information", "warning", "critical"):
        setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: 0))
    QtWidgets.QMessageBox.question = staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    QtWidgets.QMessageBox.exec = lambda self: 0
    QtWidgets.QDialog.exec = lambda self: (self.show(), 0)[1]


def load_recipes():
    import help_shots
    for m in pkgutil.iter_modules(help_shots.__path__):
        importlib.import_module("help_shots." + m.name)


def _grab(ctx, s: Shot, w):
    from PySide6 import QtCore, QtGui, QtWidgets
    w.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.resize(*s.size)
    w.show()
    ctx.settle(400)
    w.resize(*s.size)
    ctx.settle(200)
    # a passing status message ("Opened ...") times out in a few seconds;
    # the picture shows the status line under it
    bar = w.statusBar() if hasattr(w, "statusBar") else None
    if bar is not None and bar.currentMessage():
        bar.clearMessage()
        ctx.settle(50)
    target = w
    if s.crop is not None:
        target = (s.crop(w) if callable(s.crop)
                  else w.findChild(QtWidgets.QWidget, s.crop))
        if target is None:
            raise Skip("nothing to crop to (%r)" % s.crop)
    img = _small(target.grab().toImage())
    os.makedirs(os.path.dirname(s.path), exist_ok=True)
    tmp = s.path + ".tmp"
    if not img.save(tmp, "PNG", 9):
        raise RuntimeError("could not write %s" % s.path)
    os.replace(tmp, s.path)


def _small(img):
    """A 256-colour copy: interface pictures have few colours, so this keeps
    them sharp at about a fifth of the size (they ship in every installer)."""
    from PySide6 import QtCore, QtGui
    flags = (QtCore.Qt.ImageConversionFlag.ThresholdDither
             | QtCore.Qt.ImageConversionFlag.AutoColor)
    return img.convertToFormat(QtGui.QImage.Format.Format_Indexed8, flags)


def referenced_images():
    """``{(folder, id)}`` of every ``images/<id>.png`` a help page shows."""
    import re
    from ovtsuite import helpdocs
    out = set()
    roots = [("data/help", helpdocs.BASE_DIR)]
    for mid in helpdocs.known_modules():
        d = os.path.join(PKG_DIR, mid, "help")
        if os.path.isdir(d):
            roots.append(("%s/help" % mid, d))
    for folder, root in roots:
        for d, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".md"):
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        for m in re.finditer(r"!\[[^\]]*\]\(((?:\.\./)*)images/([\w.-]+)\.png\)",
                                             fh.read()):
                            out.add((folder, m.group(2)))
    return out


def _private_settings(path):
    """Every ``QSettings`` the app opens -- all but ``QSettings(file,
    format)`` -- goes to the INI file ``path``: the pictures show the defaults
    (toolbar, options, panels), and the developer's own settings are never
    read or written. (``QSettings.setPath`` does not reach ``QSettings(org,
    app)`` on Windows, which is always the registry.)"""
    from PySide6 import QtCore
    real = QtCore.QSettings
    _SETTINGS_FILE[0] = path

    class _Private(real):
        def __init__(self, *args, **kw):
            if len(args) >= 2 and isinstance(args[0], str) and \
                    isinstance(args[1], real.Format):
                super().__init__(*args, **kw)
            else:
                super().__init__(_SETTINGS_FILE[0], real.Format.IniFormat)
    QtCore.QSettings = _Private


#: the settings file in use: a fresh one for each shot, so every picture
#: starts from the defaults (a panel one recipe opened stays shut in the next)
_SETTINGS_FILE = [""]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Regenerate the help screenshots.")
    ap.add_argument("ids", nargs="*", help="only these shots")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="fail if a page shows an image no recipe makes")
    ap.add_argument("--style", default="", help="Qt style (default: the platform's)")
    ap.add_argument("--theme", default="light", choices=("light", "dark", "system"),
                    help="colour scheme of the pictures (default: light, whatever "
                         "Windows is set to)")
    a = ap.parse_args(argv)
    load_recipes()
    if a.list:
        for s in SHOTS.values():
            print("%-34s %-14s %s" % (s.id, s.folder, s.what))
        return 0
    if a.check:
        made = {(s.folder, s.id) for s in SHOTS.values()}
        missing = sorted(referenced_images() - made)
        for folder, sid in missing:
            print("no recipe for %s/images/%s.png" % (folder, sid))
        return 1 if missing else 0

    # Run offscreen (QT_QPA_PLATFORM=offscreen), Qt has no fonts and draws
    # every character as a wide box: give it the system's, as the tests do
    # (before the QApplication exists).
    if os.name == "nt" and os.environ.get("WINDIR"):
        os.environ.setdefault("QT_QPA_FONTDIR",
                              os.path.join(os.environ["WINDIR"], "Fonts"))
    from PySide6 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    if a.style:
        app.setStyle(a.style)
    if a.theme != "system":
        app.styleHints().setColorScheme(QtCore.Qt.ColorScheme.Light if a.theme == "light"
                                        else QtCore.Qt.ColorScheme.Dark)
    import tempfile
    _private_settings(os.path.join(tempfile.mkdtemp(prefix="ovt-help-settings-"),
                                   "settings.ini"))
    # the recent lists, last folders, AI settings and crash recovery too:
    # never the developer's own. They are all read through
    # recent._config_dir, as the tests' isolated_qsettings fixture redirects
    # them (APPDATA itself is left alone: Python finds the user's own
    # site-packages through it).
    cfg = tempfile.mkdtemp(prefix="ovt-help-config-")
    import atexit
    import shutil
    atexit.register(shutil.rmtree, cfg, True)
    from ovtsuite import recent
    recent._config_dir = lambda: cfg
    recent._legacy_dirs = lambda: []
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    from ovtsuite import branding, gui
    app.setApplicationName(branding.APP_NAME)
    app.setWindowIcon(gui.app_icon())
    _quiet_popups()
    from ovtsuite import recent
    projects = os.path.join(tempfile.gettempdir(), "OVT-Suite help", "Projects")
    os.makedirs(projects, exist_ok=True)
    for kind in ("binary", "log", "patch", "eeprom"):
        recent.set_last_dir(projects, kind)     # logs and files beside the projects
    ctx = Context(app)
    todo = [SHOTS[i] for i in a.ids] if a.ids else list(SHOTS.values())
    done = skipped = failed = 0
    for n, s in enumerate(todo):
        if _SETTINGS_FILE[0]:
            _SETTINGS_FILE[0] = os.path.join(os.path.dirname(_SETTINGS_FILE[0]),
                                             "settings-%d.ini" % n)
        try:
            w = s.make(ctx)
            _grab(ctx, s, w)
            done += 1
            print("  ok    %s" % os.path.relpath(s.path, ROOT))
        except Skip as ex:
            skipped += 1
            print("  skip  %s: %s" % (s.id, ex))
        except Exception as ex:                       # keep going; report
            failed += 1
            import traceback
            print("  FAIL  %s: %s" % (s.id, ex))
            traceback.print_exc(limit=3)
        finally:
            ctx.reset()
            ctx.settle(50)
    print("help_screenshots: %d written, %d skipped, %d failed" % (done, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
