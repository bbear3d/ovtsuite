# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The binaries a WinOLS project (``.ols``) carries, listed and extracted.

A project holds the binary WinOLS imported (its *original*) and every
version made from it, each stored whole, one after the other, at the end of
the file. A binary WinOLS put together from several memories (an EDC16's
external flash and its processor's internal flash, a gearbox's two areas)
lists them as areas ``#1``, ``#2`` ...: each can be extracted alone as well.

:func:`images` lists what there is, each with a key (``original``,
``version1`` ..., ``original-area1`` ...), a default file name
(``<project>_<what>.bin``, or the extension of the file WinOLS imported when
the project records one) and whether it is ticked by default (the whole
images are; the areas, which repeat them, are not). :func:`extract` writes
the chosen ones exactly as stored -- or, given an image format, as Intel HEX
or S-record.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

#: extensions a raw binary is commonly saved with (kept for an extracted
#: image when the project says the original was imported from one)
RAW_EXT = (".bin", ".ori", ".mod", ".org", ".ecu", ".dat", ".eep", ".epr",
           ".mpc", ".flash", ".rom", ".img", ".fls")


@dataclass
class Embedded:
    key: str                    # "original", "version1", "original-area2" ...
    label: str                  # "Original", "Version 'Stage 1'" ...
    name: str                   # default output file name
    size: int
    default: bool = True        # ticked by default
    version: str = ""           # the WinOLS version it belongs to
    start: int = 0              # offset in that version's image (areas)
    note: str = ""              # what the project says (imported from ...)
    data: bytes = field(default=b"", repr=False)


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip()).strip("._")
    return s.lower() or "image"


def _imported_from(comment: str) -> str:
    """The file name a version's comment says it was imported from."""
    m = re.search(r"(?:from file:?|aus Datei:?|z pliku:?)\s*(.+?)(?:\s+by\s|\s+on\s|\r|\n|$)",
                  comment or "", re.I)
    if not m:
        return ""
    return os.path.basename(m.group(1).strip().replace("\\", "/"))


def _size_text(n: int) -> str:
    if n >= 1 << 20 and n % (1 << 20) == 0:
        return "%d MB" % (n >> 20)
    if n >= 1 << 10 and n % (1 << 10) == 0:
        return "%d KB" % (n >> 10)
    return "%d bytes" % n


def images(data: bytes, stem: str) -> List[Embedded]:
    """Every image a project ``data`` carries (``stem``: the project's file
    name without extension, for the default names). [] for anything else."""
    from ..definitions import winols as W
    if not W.is_winols(data):
        return []
    wf = W.scan_images(data) or W.parse(data)
    if wf.kind != "ols" or not wf.versions:
        return []
    out: List[Embedded] = []
    used = set()
    for i, v in enumerate(wf.versions):
        src = _imported_from(v.comment)
        ext = os.path.splitext(src)[1].lower()
        ext = ext if ext in RAW_EXT else ".bin"
        what = "original" if i == 0 else _slug(v.name or "version%d" % i)
        key = "original" if i == 0 else "version%d" % i
        if what in used:
            what = "%s_%d" % (what, i)
        used.add(what)
        label = ("Original" if i == 0 else "Version") + (
            " '%s'" % v.name if v.name and (i or v.name.lower() != "original") else "")
        out.append(Embedded(key, label, "%s_%s%s" % (stem, what, ext), len(v.data),
                            True, v.name, 0, "imported from %s" % src if src else
                            (v.comment.splitlines()[0] if v.comment else ""), v.data))
        for k, (aname, a0, a1) in enumerate(wf.areas, start=1):
            if a1 >= len(v.data):
                continue
            out.append(Embedded("%s-area%d" % (key, k),
                                "%s, area %s (0x%X-0x%X)" % (label, aname, a0, a1),
                                "%s_%s_area%d.bin" % (stem, what, k), a1 - a0 + 1, False,
                                v.name, a0, "", v.data[a0:a1 + 1]))
    return out


#: sizes of a processor's internal flash that WinOLS stores as an area of
#: the original beside the external flash (EDC16: an MPC561 / 562's 448 KB,
#: MPC563 / 565's 512 KB)
MPC_SIZES = (0x70000, 0x80000)


def main_images(items: List[Embedded], stem: str) -> Dict[str, Embedded]:
    """The project's own binary and, where WinOLS stores it beside it, the
    processor's internal flash: ``{"binary": ..., "mpc": ...}`` (either may
    be missing), each with a default name ``<stem>.bin`` / ``<stem>.mpc``.

    An original of two areas, one of them an internal-flash size, is the
    external flash (the binary) and the MPC; an original with no areas is
    the binary as it is."""
    import dataclasses
    orig = next((e for e in items if e.key == "original"), None)
    if orig is None:
        return {}
    areas = [e for e in items if e.key.startswith("original-area")]
    out: Dict[str, Embedded] = {}
    if len(areas) == 2:
        mpc = [a for a in areas if a.size in MPC_SIZES]
        if len(mpc) == 1:
            flash = next(a for a in areas if a is not mpc[0])
            out["binary"] = dataclasses.replace(flash, name=stem + ".bin")
            out["mpc"] = dataclasses.replace(mpc[0], name=stem + ".mpc")
            return out
    if not areas:
        ext = os.path.splitext(orig.name)[1] or ".bin"
        out["binary"] = dataclasses.replace(orig, name=stem + ext)
    return out


def summary(path: str, data: Optional[bytes] = None) -> str:
    """A line saying what a WinOLS project holds, for the file list:
    ``"WinOLS project: 7290 maps; embeds a 256 KB binary + 2 versions"``."""
    from ..definitions import winols as W
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    if not W.is_winols(data):
        return ""
    try:
        wf = W.scan_images(data) or W.parse(data)
    except Exception:                                   # noqa: BLE001
        return "WinOLS file (not readable)"
    if wf.kind != "ols":
        return "WinOLS map pack: %d maps, no binary" % wf.declared
    bits = ["WinOLS project: %d maps" % wf.declared]
    if wf.versions:
        img = "embeds a %s binary" % _size_text(len(wf.versions[0].data))
        more = len(wf.versions) - 1
        if more:
            img += " + %d version%s (%s)" % (more, "" if more == 1 else "s", ", ".join(
                v.name or "?" for v in wf.versions[1:4]) + (", ..." if more > 3 else ""))
        if wf.areas:
            img += " in %d areas" % len(wf.areas)
        bits.append(img)
    else:
        bits.append("no binary")
    return "; ".join(bits)


def select(items: List[Embedded], only: Optional[Iterable] = None) -> List[Embedded]:
    """The items ``only`` names (keys, labels, version names or file
    names, case-blind; ``"all"`` for every one), or the default ones."""
    if only is None or only == "default":
        return [e for e in items if e.default]
    if only == "all":
        return list(items)
    want = [str(w).strip().lower() for w in only]
    if "all" in want:
        return list(items)
    out = []
    for e in items:
        names = {e.key.lower(), e.label.lower(), e.name.lower(),
                 os.path.splitext(e.name)[0].lower()}
        if e.version and not e.start and "area" not in e.key:
            names.add(e.version.lower())
        if names & set(want):
            out.append(e)
    return out


def extract(sources: List[str], out_dir: str, only=None, fmt: str = "raw",
            names: Optional[Dict[str, str]] = None, record: int = 32,
            inputs: Iterable[str] = ()) -> List[tuple]:
    """Write the chosen images of every project in ``sources`` into
    ``out_dir``: ``[(path, Embedded)]``. ``only``: see :func:`select`, or a
    list of ``{"source", "key", "name"}`` picks (the window's). ``fmt``:
    ``raw`` (the bytes as stored, default), ``bin`` (the same, named .bin),
    ``hex`` or ``srec``. ``names``: ``{key: file name}`` overrides. A file
    being read is never overwritten."""
    from . import ConvertError, _key
    from . import image as _image
    os.makedirs(out_dir or ".", exist_ok=True)
    keys_in = {_key(p) for p in list(sources) + list(inputs) if p}
    picks = None
    if isinstance(only, list) and only and isinstance(only[0], dict):
        picks = {}
        for p in only:
            picks.setdefault(_key(p.get("source", "")), {})[p.get("key")] = p.get("name") or ""
    done = []
    for src in sources:
        with open(src, "rb") as fh:
            data = fh.read()
        stem = os.path.splitext(os.path.basename(src))[0]
        items = images(data, stem)
        if not items:
            continue
        if picks is not None:
            mine = picks.get(_key(src), {})
            chosen = [e for e in items if e.key in mine]
            over = {k: v for k, v in mine.items() if v}
        else:
            chosen = select(items, only)
            over = names or {}
        for e in chosen:
            name = over.get(e.key) or e.name
            if fmt in ("hex", "srec"):
                name = os.path.splitext(name)[0] + (".hex" if fmt == "hex" else ".s19")
            elif fmt == "bin":
                name = os.path.splitext(name)[0] + ".bin"
            path = os.path.join(out_dir, name)
            if _key(path) in keys_in:
                raise ConvertError("Extracting %s would overwrite %s, which is being read. "
                                   "Choose another name or folder."
                                   % (e.label, os.path.basename(path)))
            if fmt in ("hex", "srec"):
                payload = _image.write(_image.Image([(e.start, e.data)], 0xFF, "bin"),
                                       fmt, record)
            else:
                payload = e.data
            with open(path, "wb") as fh:
                fh.write(payload)
            done.append((path, e))
    return done
