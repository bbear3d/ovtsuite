# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 boot-sector database + virtual boot read.

The boot/low region (0x10000-0x3FEFF) is not readable over OBD on these ECUs,
so a full read leaves it 0xFF-padded. But the boot is bootloader code that is
shared across a whole hardware/project family: across 327 factory SGOs (the
SGOUnpack corpus) the 267 present boots reduce to just **13 unique code
templates** once the per-SW fields are set aside. Each individual boot differs
from its template only inside two small windows -- the dataset-id/header area
(0x150-0x17F) and a trailer/signature block (0x2FE00-0x2FEFF) -- so a boot is
exactly `template + a 0x130-byte per-SW patch`.

This lets us offer a **virtual read**: match the target ECU's dataset-id (or VAG
part number, both readable from the program/cal area) against the database and
reconstruct its boot. An exact dataset-id match reproduces the original boot
byte-for-byte; a part/project match returns the right code template with a
reference SW's id/signature (flagged approximate -- correct code, but its
metadata/signature belong to another SW, so it is for backup/analysis, not a
guaranteed re-flash).

The database is built offline from the corpus into ``edc16_bootdb.bin``:
zlib-compressed templates + a JSON index of entries.

It is an ADD-ON, not part of the app or of any module: the templates are
Bosch's boot code, so the file is never inside an installer. The Windows
installers offer to download it (packaging/addon_bootdb.iss, from the URL in
``addons/addons.json``); otherwise it is placed by hand. :func:`path` finds it
in, in order: ``$OVTSUITE_BOOTDB``, the user's config folder's ``addons``
(``%APPDATA%/OVT-Suite/addons``, ``~/.config/ovtsuite/addons``), the
installation's ``addons`` folder beside the executable, and -- running from
source -- the repository's ``addons`` folder. Without it the virtual boot read
simply leaves the boot region 0xFF.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from collections import Counter

_MAGIC = b"OVTBOOT1"
#: The add-on's file name, wherever it is installed.
DB_NAME = "edc16_bootdb.bin"

BOOT_ADDR = 0x10000
BOOT_LEN = 0x2FF00              # 0x10000..0x3FEFF inclusive

_cache = None


class BootDB:
    def __init__(self, templates, index):
        self.templates = templates            # list[bytes] -- code-normalized boots
        self.id_win = tuple(index["id_win"])
        self.tr_win = tuple(index["tr_win"])
        self.boot_addr = index.get("boot_addr", BOOT_ADDR)
        self.boot_len = index.get("boot_len", BOOT_LEN)
        self.entries = index["entries"]       # list of {did, part, proj, tpl, id, tr}
        self._by_did = {e["did"]: e for e in self.entries if e.get("did")}
        self._by_sw = {}
        self._by_part = {}
        self._by_proj = {}
        for e in self.entries:
            sw = (e.get("did") or "")[:10]
            if sw.isdigit():
                e["sw"] = sw
                self._by_sw.setdefault(sw, e)
            if e.get("part"):
                self._by_part.setdefault(e["part"], []).append(e)
            if e.get("proj"):
                self._by_proj.setdefault(e["proj"], []).append(e)

    # -- reconstruction --------------------------------------------------
    def _reconstruct(self, entry, id_override=None):
        boot = bytearray(self.templates[entry["tpl"]])
        idb = bytes.fromhex(entry["id"])
        boot[self.id_win[0]:self.id_win[1]] = idb
        boot[self.tr_win[0]:self.tr_win[1]] = bytes.fromhex(entry["tr"])
        if id_override:
            # overwrite only the ASCII dataset-id string at the start of the id
            # window (leave the header checksum bytes from the reference)
            s = id_override.encode("ascii", "replace")[: self.id_win[1] - self.id_win[0]]
            boot[self.id_win[0]:self.id_win[0] + len(s)] = s
        return bytes(boot)

    def _pick(self, entries):
        """Most-common template among `entries`, and a representative entry of it."""
        tpl = Counter(e["tpl"] for e in entries).most_common(1)[0][0]
        rep = next(e for e in entries if e["tpl"] == tpl)
        return rep

    def virtual_boot(self, dataset_id=None, part=None, project=None):
        """Return (boot_bytes, quality, source) or None.

        quality: 'exact' (byte-identical to the original), 'part' or 'project'
        (correct code template, id/signature from a reference SW -- approximate).
        source: the reference dataset-id/part the boot came from.
        """
        sw = (dataset_id or "")[:10]
        if sw and sw.isdigit() and sw in self._by_sw:
            e = self._by_sw[sw]
            return self._reconstruct(e), "exact", e.get("did") or sw
        if dataset_id and dataset_id in self._by_did:
            e = self._by_did[dataset_id]
            return self._reconstruct(e), "exact", dataset_id
        if part and part in self._by_part:
            e = self._pick(self._by_part[part])
            return self._reconstruct(e, id_override=dataset_id), "part", e.get("did") or part
        if project and project in self._by_proj:
            e = self._pick(self._by_proj[project])
            return self._reconstruct(e, id_override=dataset_id), "project", e.get("did") or project
        return None

    def parts(self):
        return sorted(self._by_part)

    def has(self, dataset_id=None, part=None, project=None):
        sw = (dataset_id or "")[:10]
        return bool((sw and sw in self._by_sw)
                    or (dataset_id and dataset_id in self._by_did)
                    or (part and part in self._by_part)
                    or (project and project in self._by_proj))


def identify(image):
    """Extract (dataset_id, part, project) from a read EXTERNAL image, the same
    way the DB keys were derived, so a virtual-read lookup matches. Uses
    fileinfo.ident16 (block header dataset_id + VAG part)."""
    from . import fileinfo as _fi
    ident = _fi.ident16(bytes(image))
    sw = ident.get("bosch_sw") or ""            # 10-digit SW number
    did = (sw + (ident.get("dataset_id") or "")) or None   # e.g. 1037382461P475AAXE
    part = ident.get("part") or ident.get("hw_part")
    proj = ident.get("project")
    return did, part, proj


def search_dirs():
    """Where the add-on may be, in the order :func:`path` looks."""
    import sys
    from . import branding
    from .recent import _config_dir
    out = [os.path.join(_config_dir(), "addons")]
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "addons"))
    pkg = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(os.path.dirname(pkg), "addons"))      # source checkout
    env = os.environ.get(branding.ENV_PREFIX + "_BOOTDB")
    if env:
        out.insert(0, env if os.path.isdir(env) else os.path.dirname(env))
    return out


def path():
    """The installed boot DB add-on, or None."""
    from . import branding
    env = os.environ.get(branding.ENV_PREFIX + "_BOOTDB")
    if env and os.path.isfile(env):
        return env
    for d in search_dirs():
        p = os.path.join(d, DB_NAME)
        if os.path.isfile(p):
            return p
    return None


def missing_text():
    """What to tell the user when the add-on is not installed."""
    from .recent import _config_dir
    return ("the EDC16 boot-sector database add-on is not installed (tick its "
            "download in the installer, or put %s in %s)"
            % (DB_NAME, os.path.join(_config_dir(), "addons")))


def load(path=None):
    global _cache
    if _cache is not None:
        return _cache
    path = path or globals()["path"]()
    if not path:
        raise FileNotFoundError(missing_text())
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != _MAGIC:
        raise ValueError("not an EDC16 boot DB")
    nt, ne = struct.unpack_from("<HH", data, 8)
    off = 12
    templates = []
    for _ in range(nt):
        (clen,) = struct.unpack_from("<I", data, off); off += 4
        templates.append(zlib.decompress(data[off:off + clen])); off += clen
    (ilen,) = struct.unpack_from("<I", data, off); off += 4
    index = json.loads(zlib.decompress(data[off:off + ilen]))
    _cache = BootDB(templates, index)
    return _cache


def available():
    try:
        return path() is not None
    except Exception:
        return False
