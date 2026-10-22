# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The help pages (ovtsuite.helpdocs) -- Qt-free.

The same Markdown is the in-app help and the website, so these hold the pages
to what both need: every page loads, every link leads somewhere, every F1
context names a real window, and every module brings its own pages.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import branding, helpdocs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _all():
    return helpdocs.HelpIndex(modules="all")


def test_front_matter():
    meta, body = helpdocs.split_front_matter(
        "---\ntitle: A page\norder: 5\ncontext: FooDialog, BarDialog\n---\n# Heading\n")
    assert meta == {"title": "A page", "order": "5", "context": "FooDialog, BarDialog"}
    assert body == "# Heading\n"
    assert helpdocs.split_front_matter("# No header\n") == ({}, "# No header\n")


def test_base_and_every_module_have_pages():
    idx = _all()
    assert idx.home() is not None and idx.home().id == "index"
    for mid in helpdocs.known_modules():
        assert idx.get("modules/%s/index" % mid) is not None, \
            "the %s module has no help/index.md" % mid


def test_every_link_leads_somewhere():
    idx = _all()
    bad = []
    for p in idx.pages.values():
        for href in LINK.findall(p.text):
            kind, target = idx.resolve(p.id, href)
            if kind == "external" or kind == "anchor":
                continue
            if kind == "page" and target not in idx.pages:
                bad.append("%s -> %s" % (p.id, href))
            if kind == "file" and not os.path.exists(target):
                bad.append("%s -> %s (missing file)" % (p.id, href))
    assert not bad, "\n".join(bad)


def test_every_context_names_a_real_window():
    defined = set()
    for d, _dirs, files in os.walk(os.path.join(ROOT, branding.PKG)):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    defined |= set(re.findall(r"^\s*class\s+(\w+)", fh.read(), re.M))
    missing = {(p.id, c) for p in _all().pages.values() for c in p.context
               if c not in defined}
    assert not missing, missing


def test_f1_context_lookup():
    idx = _all()
    assert idx.for_context(["FlashDialog", "QDialog"]).id == "flashing"
    assert idx.for_context(["MainWindow"]).id == "interface"
    assert idx.for_context(["NoSuchDialog"]) is None


def test_links_resolve_like_the_website():
    idx = _all()
    kind, target = idx.resolve("modules/vdo/index", "images/vdo-sweep.png")
    assert kind == "file" and target.endswith("vdo-sweep.png")
    assert idx.resolve("modules", "modules/edc16/index.md") == ("page", "modules/edc16/index")
    assert idx.resolve("modules/edc16/index", "../../logging.md") == ("page", "logging")
    assert idx.resolve("index", "https://example.com/x")[0] == "external"
    assert idx.resolve("editing", "interface.md#tabs") == ("page", "interface")


def test_an_absent_modules_page_says_what_to_install(monkeypatch):
    idx = helpdocs.HelpIndex(modules=[])
    assert all(not p.module for p in idx.pages.values())
    kind, target = idx.resolve("modules", "modules/zf8hp/index.md")
    assert kind == "page" and idx.get(target) is None
    text = idx.missing_text(target)
    assert helpdocs.known_modules()["zf8hp"] in text and "Tools > Modules" in text


def test_contents_put_module_help_under_the_modules_page():
    toc = _all().toc()
    mods = toc[-1]
    assert mods.page is not None and mods.page.id == "modules"
    names = [s.title for s in mods.children]
    assert names[0] == helpdocs.known_modules()["edc16"]
    assert "modules" not in [s.page.id for s in toc[:-1] if s.page is not None]


def test_search():
    hits = _all().search("version history")
    assert "editing" in [p.id for p, _s in hits[:5]]
    assert all("version" in (p.title + p.text).lower() for p, _s in hits)
    assert _all().search("   ") == []


def test_online_url(monkeypatch):
    monkeypatch.setattr(helpdocs, "settings", lambda: {"online_url": "https://x.com/help/"})
    assert helpdocs.online_url("") == "https://x.com/help/"
    assert helpdocs.online_url("flashing") == "https://x.com/help/flashing/"
    assert helpdocs.online_url("modules/edc16/index") == "https://x.com/help/modules/edc16/"
    monkeypatch.setattr(helpdocs, "settings", lambda: {})
    assert helpdocs.online_url("flashing") == ""


def test_module_help_belongs_to_the_module():
    """A module's help ships in its .ovtmod, not in the base app."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from ovtsuite.modules import build
    owners = build.ownership(os.path.join(ROOT, branding.PKG))
    assert owners["edc16/help/index.md"] == "edc16"
    assert owners["data/help/index.md"] == ""


def test_website_staging(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import json
    import build_wiki
    idx = build_wiki.stage(str(tmp_path), "https://example.com/help")
    docs = tmp_path / "docs"
    assert (docs / "index.md").is_file()
    assert (docs / "modules" / "edc16" / "index.md").is_file()
    assert not (docs / "help.json").exists()
    text = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    cfg = json.loads(text.split("\n", 1)[1])          # YAML is a superset of JSON
    assert cfg["site_url"] == "https://example.com/help/"
    flat = json.dumps(cfg["nav"])
    for pid in idx.pages:
        assert pid + ".md" in flat, pid


def test_the_shift_editors_have_their_page_and_the_full_load_step_its_own():
    from ovtsuite import helpdocs
    idx = helpdocs.HelpIndex()
    assert idx.for_context(["GearboxEditDialog"]).title == "Shift map and lock-up editors"
    assert idx.for_context(["FullLoadPanel", "GearboxEditDialog"]).title == \
        "Full-load shift points"
