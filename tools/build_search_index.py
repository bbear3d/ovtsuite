#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Build ovtsuite/data/search_index.json.gz -- the shipped label cache.

Search ranks parameters by their English tree label. Making a label is the one
slow step of preparing a file's search text (translation and tidying, ~0.4 s
for a 10,000-parameter image), and it depends only on a parameter's symbol and
description -- both of which are in the definition files that ship with the
app. So it is done here, once, at build time, and looked up by a hash of
(symbol, description, label) when a file is loaded.

The index is advisory and self-checking:

* it records the digest of the definition data and of the code that makes
  labels (``gui.py`` and the translators), and the loader ignores it if either
  has changed since it was built;
* a parameter whose symbol, description or label differ from what was indexed
  simply misses and has its label made the normal way.

A stale or absent index therefore only costs time; it can never put a wrong
label in a result.

    python tools/build_search_index.py            # write the index
    python tools/build_search_index.py --check    # exit 1 if it is stale
    python tools/build_search_index.py --stats    # sizes and coverage
"""

import argparse
import glob
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ovtsuite import kb as kbmod                                   # noqa: E402
from ovtsuite import search as S                                   # noqa: E402
from ovtsuite.model import Entity                                  # noqa: E402


def _stand_in(name, kind, categories, description, units):
    """The parts of an Entity the label is made from; the rest is placement."""
    return Entity(name=name, kind=kind, tier=3, categories=categories,
                  description=description, units=units, addr=0, rows=1, cols=1,
                  bits=16, signed=False, equation="X", source="")


def families():
    """Every definition file in the KB layout (``tables`` + ``constants``
    arrays). EDC15 and the ZF layouts are built differently and are not
    cached: their labels come from the file's own stored definition or are
    structural, and they are left to the normal path."""
    out = []
    for p in sorted(glob.glob(os.path.join(S._data_dir(), "*.json.gz"))):
        fam = os.path.basename(p)[:-len(".json.gz")]
        # The EDC15 and ZF 6HP definition files use their own record layouts
        # (kb15 / kb6hp: a ZF table record has 29 fields, an EDC16 KbTable at
        # most 26), so kb.load cannot read them; the family names are how the
        # app itself keeps the platforms apart.
        if fam.startswith(("edc15_", "zf6hp")) or fam in ("layouts", "search_index"):
            continue
        out.append(fam)
    return out


def reference_images():
    """Binaries to read labels from for the platforms the KB files do not
    describe by symbol alone: the EDC15 images and the ZF 6HP images. Opened
    exactly as the app opens them, so the entities -- and so the labels -- are
    the real ones. Missing reference material just means less is cached."""
    ref = os.path.join(ROOT, "Reference")
    out = []
    # The OEM pack is sub-foldered by family (OEM/6HP28/ holds the MPC562
    # images), so one level of sub-directory is read too.
    for sub in (os.path.join("Binaries", "EDC15"), "ZF6HP",
                os.path.join("ZF6HP", "OEM"),
                os.path.join("ZF6HP", "OEM", "6HP28")):
        d = os.path.join(ref, sub)
        if os.path.isdir(d):
            out += sorted(os.path.join(d, n) for n in os.listdir(d)
                          if n.lower().endswith(".bin"))
    return out


def _one_image(path):
    """(family, [(key, label)]) for one image, or None if it will not open."""
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    try:
        sess = TunerSession(path)
        ents = S.SearchEngine.entities_of(sess)
        return (getattr(sess, "family", "?"),
                [(S.title_key(e), gui._english(e)) for e in ents
                 if not getattr(e, "plain", "")
                 and not getattr(e, "coding", "")])
    except Exception as ex:
        # The app refuses these the same way (e.g. an internal-ROM dump is not a
        # calibration image): say why, so a skip reads as a reason, not a fault.
        print("  skip %s (%s: %s)" % (os.path.basename(path), type(ex).__name__,
                                      str(ex).splitlines()[0][:100] if str(ex) else ""))
        return None


def _image_pairs(images, cache, budget):
    """Yield (path, family, pairs) for each reference image. Results are kept
    per image in ``cache`` (a directory) so a long build can be stopped and
    resumed; ``budget`` is seconds after which it stops with exit code 3."""
    import time
    from multiprocessing import Pool
    if not images:
        return
    t0 = time.time()
    paths = reference_images()
    if cache:
        os.makedirs(cache, exist_ok=True)

    def cpath(p):
        st = os.stat(p)
        return os.path.join(cache, "%s_%d_%d.json" % (
            os.path.basename(p), st.st_size, int(st.st_mtime)))
    todo = [p for p in paths if not (cache and os.path.exists(cpath(p)))]
    if todo:
        with Pool(min(len(todo), os.cpu_count() or 1)) as pool:
            for p, r in zip(todo, pool.imap(_one_image, todo)):
                if cache:
                    with open(cpath(p), "w") as fh:
                        json.dump(r, fh)
                if budget and time.time() - t0 > budget:
                    pool.terminate()
                    print("  time budget reached; re-run to resume")
                    raise SystemExit(3)
    for p in paths:
        if cache and os.path.exists(cpath(p)):
            with open(cpath(p)) as fh:
                r = json.load(fh)
        else:
            r = _one_image(p)
        if r:
            yield p, r[0], r[1]


def build(images=True, cache=None, budget=None):
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    titles = {}
    ambiguous = set()
    n_rows = 0
    used = []

    def add(e):
        if getattr(e, "plain", "") or getattr(e, "coding", ""):
            # a catalogue name can carry a per-file selector note: made live
            return
        add_pair(S.title_key(e), gui._english(e))

    def add_pair(k, t):
        if k in ambiguous:
            return
        old = titles.setdefault(k, t)
        if old != t:                    # same key, different label: leave the
            del titles[k]               # label to be made live, never guess
            ambiguous.add(k)
    for fam in families():
        try:
            kb = kbmod.load(fam)
        except Exception as ex:                  # not a KB-layout file
            print("  skip %-14s (%s: %s)" % (fam, type(ex).__name__, str(ex)[:100]))
            continue
        used.append(fam)
        from ovtsuite.tiers import place
        for t in kb.tables:
            e = _stand_in(t.name, t.kind, t.categories, t.description, t.units)
            place(e, t.cat)             # the tree path is part of the key
            n_rows += 1
            add(e)
        for c in kb.constants:
            e = _stand_in(c.name, "constant", c.categories, c.description,
                          c.units)
            place(e, c.cat)
            n_rows += 1
            add(e)
    n_images = 0
    for path, fam, pairs in _image_pairs(images, cache, budget):
        used.append("image:%s" % fam)
        n_images += 1
        for k, t in pairs:
            n_rows += 1
            add_pair(k, t)
    if ambiguous:
        print("  %d keys left out (one key, two labels)" % len(ambiguous))
    data_d, ui_d = S.current_digests()
    return {"format": S.FORMAT, "data": data_d, "ui": ui_d,
            "families": used, "images": n_images,
            "titles": dict(sorted(titles.items()))}, n_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the shipped index is missing or stale")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--no-images", action="store_true",
                    help="definition files only; skip the reference binaries")
    ap.add_argument("--cache", help="directory for per-image results "
                    "(lets an interrupted build resume)")
    ap.add_argument("--budget", type=float, help="stop after N seconds")
    ap.add_argument("--out", default=S.index_path())
    a = ap.parse_args()

    if a.check:
        ok = S.load_shipped_titles(a.out) is not None
        print("search index:", "current" if ok else "MISSING or STALE")
        return 0 if ok else 1

    idx, n_rows = build(images=not a.no_images, cache=a.cache, budget=a.budget)
    tmp = a.out + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, a.out)
    size = os.path.getsize(a.out)
    print("search index: %d labels for %d definitions across %d sources "
          "(%d reference images), %.0f KB -> %s" % (
              len(idx["titles"]), n_rows, len(idx["families"]),
              idx["images"], size / 1024.0, a.out))
    if a.stats:
        print("  deduplicated %d -> %d (%.1fx)" % (
            n_rows, len(idx["titles"]), n_rows / max(1, len(idx["titles"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
