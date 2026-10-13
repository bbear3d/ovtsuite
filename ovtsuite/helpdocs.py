# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The help pages: one Markdown source for the in-app help and the website.

Where pages live
----------------
* the base app's: ``ovtsuite/data/help/**.md``;
* a module's: ``ovtsuite/<module>/help/**.md`` -- inside the module's own
  package, so the module build packs them into its ``.ovtmod``, its signature
  covers them, and installing or removing the module adds or removes its help.

Every page has an **id**: its path under the help tree without ``.md``. Base
pages sit at the top (``flashing``, ``getting-started``); a module's pages sit
under ``modules/<module id>/`` (``modules/edc16/index``). The website is built
with exactly the same tree (tools/build_wiki.py), so a relative link such as
``[EEPROM editor](modules/edc16/eeprom.md)`` works in both places.

A page may start with a small header (front matter)::

    ---
    title: ECU read / write
    order: 30
    context: FlashDialog, PairHeadUnitDialog
    ---

``title`` (default: the first ``# heading``), ``order`` (position among its
siblings, default 100) and ``context`` -- the dialog or window classes the page
documents, so F1 in one of them opens it. Modules use the same keys for their
own dialogs; nothing else needs registering.

Pages are Markdown only: no scripts, no HTML that runs. Images sit beside the
pages and are linked relatively. Links to other sites open in the browser.
"""

from __future__ import annotations

import importlib.util
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from . import branding

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "help")
MODULES_PREFIX = "modules/"
DEFAULT_ORDER = 100
_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)


@dataclass
class Page:
    id: str
    path: str                     # the .md file
    title: str
    order: int = DEFAULT_ORDER
    context: Tuple[str, ...] = ()
    module: str = ""              # owning module id ("" = base app)
    text: str = field(default="", repr=False)   # body, front matter removed

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class Section:
    """A node of the contents: a page (or none), and pages under it."""
    title: str
    page: Optional[Page] = None
    children: List["Section"] = field(default_factory=list)
    order: int = DEFAULT_ORDER


def split_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """``({key: value}, body)``. Only ``key: value`` lines are understood."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() and not k.strip().startswith("#"):
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def _load_page(path: str, pid: str, module: str) -> Page:
    meta, body = split_front_matter(_read(path))
    title = meta.get("title")
    if not title:
        h = re.search(r"^#\s+(.+?)\s*#*\s*$", body, re.M)
        title = h.group(1) if h else os.path.splitext(os.path.basename(path))[0]
    try:
        order = int(meta.get("order", DEFAULT_ORDER))
    except ValueError:
        order = DEFAULT_ORDER
    ctx = tuple(c.strip() for c in meta.get("context", "").split(",") if c.strip())
    return Page(pid, path, title, order, ctx, module, body)


def _pages_in(root: str, prefix: str, module: str) -> List[Page]:
    out = []
    if not os.path.isdir(root):
        return out
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith((".", "_")))
        for f in sorted(files):
            if f.lower().endswith(".md"):
                p = os.path.join(d, f)
                rel = os.path.relpath(p, root).replace(os.sep, "/")[:-3]
                out.append(_load_page(p, prefix + rel, module))
    return out


def module_help_dir(module_id: str) -> Optional[str]:
    """The ``help`` folder of a module, wherever it is installed (None if the
    module is absent or has no help). Does not import the module."""
    try:
        spec = importlib.util.find_spec("%s.%s" % (branding.PKG, module_id))
    except (ImportError, ValueError):
        return None
    locs = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
    for loc in locs:
        d = os.path.join(loc, "help")
        if os.path.isdir(d):
            return d
    return None


def known_modules() -> Dict[str, str]:
    """module id -> display name, for every module the app knows."""
    try:
        from .platforms import MODULES
        return {mid: m.name for mid, m in MODULES.items()}
    except Exception:
        return {}


def installed_modules() -> List[str]:
    try:
        from .modules import registry
        return sorted(registry.installed())
    except Exception:
        return []


class HelpIndex:
    """Every page of the base app plus those of ``modules`` (default: the
    installed ones). ``modules="all"`` takes every module found in the tree --
    what the website is built from."""

    def __init__(self, modules: Optional[Iterable[str]] = None, base_dir: str = BASE_DIR):
        if modules == "all":
            modules = list(known_modules())
        elif modules is None:
            modules = installed_modules()
        order = list(known_modules())
        self.modules = sorted(modules, key=lambda m: (order.index(m) if m in order
                                                      else len(order), m))
        self.pages: Dict[str, Page] = {}
        for p in _pages_in(base_dir, "", ""):
            self.pages[p.id] = p
        self.module_dirs: Dict[str, str] = {}
        for mid in self.modules:
            d = module_help_dir(mid)
            if d is None:
                continue
            self.module_dirs[mid] = d
            for p in _pages_in(d, MODULES_PREFIX + mid + "/", mid):
                self.pages[p.id] = p

    # -- lookup ------------------------------------------------------------
    def get(self, pid: str) -> Optional[Page]:
        return self.pages.get(pid)

    def home(self) -> Optional[Page]:
        return self.pages.get("index") or next(iter(self.pages.values()), None)

    def for_context(self, class_names: Iterable[str]) -> Optional[Page]:
        """The page documenting the first of ``class_names`` (most specific
        first: a window's class, then its base classes)."""
        for name in class_names:
            for p in self.pages.values():
                if name in p.context:
                    return p
        return None

    def module_of(self, pid: str) -> str:
        if pid.startswith(MODULES_PREFIX):
            return pid[len(MODULES_PREFIX):].split("/", 1)[0]
        return ""

    def missing_text(self, pid: str) -> str:
        """Markdown for a link to a page of a module that is not installed."""
        mid = self.module_of(pid)
        name = known_modules().get(mid, mid or "another")
        return ("# %s module\n\nThis page comes with the **%s** module, which is not "
                "installed.\n\nInstall it from **Tools > Modules...** (or with its own "
                "installer), and its help appears here.\n" % (name, name))

    # -- links -------------------------------------------------------------
    def resolve(self, from_id: str, href: str) -> Tuple[str, str]:
        """``(kind, target)`` for a link on page ``from_id``.

        kind ``"external"`` (target: the URL), ``"page"`` (target: a page id,
        which may name an absent module's page -- see :meth:`missing_text`),
        ``"anchor"`` (target: the fragment on the same page) or ``"file"``
        (target: an absolute path, e.g. a linked image or download)."""
        if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) and not re.match(r"^[a-z]:[\\/]", href, re.I):
            return "external", href
        path, _, frag = href.partition("#")
        if not path:
            return "anchor", frag
        base = posixpath.dirname(from_id)
        target = posixpath.normpath(posixpath.join(base, path))
        if target.lower().endswith(".md"):
            target = target[:-3]
        if target.endswith("/") or target in ("", "."):
            target = (target.rstrip("/") + "/index").lstrip("/")
        is_page = not posixpath.splitext(target)[1]      # images etc. are files
        if target in self.pages or (is_page and target.startswith(MODULES_PREFIX)):
            return "page", target
        if posixpath.join(target, "index") in self.pages:
            return "page", posixpath.join(target, "index")
        page = self.pages.get(from_id)
        if page is not None:
            return "file", os.path.normpath(os.path.join(page.dir, path))
        return "page", target

    # -- contents ----------------------------------------------------------
    def toc(self) -> List[Section]:
        """The contents tree: the base pages (folders as sections), then one
        section per module, headed by its ``index`` page."""
        root = Section("")
        base = [p for p in self.pages.values() if not p.module]
        _build(root, base, "")
        mods = []
        for mid in self.modules:
            pages = [p for p in self.pages.values() if p.module == mid]
            if not pages:
                continue
            sec = Section(known_modules().get(mid, mid), order=DEFAULT_ORDER)
            _build(sec, pages, MODULES_PREFIX + mid + "/")
            if sec.page is None and sec.children and sec.children[0].page is not None \
                    and sec.children[0].page.id.endswith("/index"):
                sec.page = sec.children.pop(0).page
            mods.append(sec)
        out = root.children
        if mods:
            # the base "modules" page heads the section of module help
            overview = next((s for s in out if s.page is not None
                             and s.page.id == "modules"), None)
            if overview is not None:
                out.remove(overview)
            out.append(Section(overview.title if overview else "Modules",
                               overview.page if overview else None,
                               mods, order=10 ** 6))
        return out

    # -- search ------------------------------------------------------------
    def search(self, query: str, limit: int = 50) -> List[Tuple[Page, str]]:
        """Pages containing every word of ``query``, best first, each with a
        one-line snippet around the first hit."""
        words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
        if not words:
            return []
        scored = []
        for p in self.pages.values():
            title, body = p.title.lower(), p.text.lower()
            if not all(w in title or w in body for w in words):
                continue
            score = sum(10 for w in words if w in title) + sum(body.count(w) for w in words)
            scored.append((-score, p.title.lower(), p))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [(p, _snippet(p.text, words[0])) for _s, _t, p in scored[:limit]]


def _build(parent: Section, pages: List[Page], prefix: str):
    """Arrange ``pages`` (ids under ``prefix``) as ``parent``'s children.

    A folder becomes a section. Its head is the page of the same name beside
    it (``flashing.md`` heads ``flashing/``), else the folder's own
    ``index.md``; a folder with neither is a plain heading."""
    here, folders = [], {}
    for p in pages:
        rest = p.id[len(prefix):]
        if "/" in rest:
            folders.setdefault(rest.split("/", 1)[0], []).append(p)
        elif rest == "index" and prefix:
            parent.page = p
            parent.title = parent.title or p.title
        else:
            here.append(p)
    kids = []
    for name, sub in folders.items():
        sec = Section(name.replace("-", " ").title())
        _build(sec, sub, prefix + name + "/")
        head = next((p for p in here if p.id == prefix + name), None)
        if head is not None:
            here.remove(head)
            if sec.page is not None:          # the folder's index too: first child
                sec.children.insert(0, Section(sec.page.title, sec.page, order=-1))
            sec.page = head
        if sec.page is not None:
            sec.title, sec.order = sec.page.title, sec.page.order
        kids.append(sec)
    kids += [Section(p.title, p, order=p.order) for p in here]
    kids.sort(key=lambda s: (s.order, s.title.lower()))
    parent.children.extend(kids)


def _snippet(text: str, word: str, width: int = 90) -> str:
    lines = [re.sub(r"^\s*(>\s?|[-*+]\s+|\d+\.\s+)", "", ln) for ln in text.splitlines()
             if not ln.lstrip().startswith("#") and not set(ln.strip()) <= set("|-: ")]
    flat = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", "\n".join(lines))   # links -> text
    flat = re.sub(r"[*`|\[\]]", "", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    i = flat.lower().find(word)
    if i < 0:
        return flat[:width]
    start = max(0, i - width // 3)
    if start:
        sp = flat.rfind(" ", 0, start)
        start = sp + 1 if sp >= 0 else start
    s = flat[start:start + width]
    if start + width < len(flat) and " " in s:
        s = s[:s.rfind(" ")]
    return ("..." if start else "") + s + ("..." if start + width < len(flat) else "")


def settings() -> dict:
    """``data/help/help.json``: ``online_url`` (the website's address)."""
    try:
        with open(os.path.join(BASE_DIR, "help.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def online_url(pid: str = "") -> str:
    """The website address of page ``pid`` ("" when no site is configured)."""
    base = (settings().get("online_url") or "").rstrip("/")
    if not base:
        return ""
    if not pid or pid == "index":
        return base + "/"
    if pid.endswith("/index"):
        return "%s/%s/" % (base, pid[:-len("/index")])
    return "%s/%s/" % (base, pid)
