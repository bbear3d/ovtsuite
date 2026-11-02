# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Installing definition packs into the user's definitions folder
(``load.user_dir()``), from a pack directory, a ``.zip`` of one (what the
contribution repository's CI publishes) or a compiled ``pack.json.gz``.

A pack is validated before anything is copied: errors refuse the install,
warnings are returned for the caller to show. An installed pack with the
same id is replaced; its previous copy is kept beside it as
``<id>.previous`` until the next install replaces that too, so a bad update
can be undone by hand.
"""
import gzip
import os
import re
import shutil
import tempfile
import zipfile
from typing import List, Optional, Tuple

from . import load as _load
from .model import Pack
from .validate import Report, validate_pack
from .yamlio import DefsError

PREVIOUS_SUFFIX = _load.PREVIOUS_SUFFIX


class InstallError(Exception):
    pass


def _pack_root_in(tree: str) -> str:
    """The pack directory inside an unpacked archive: the tree itself, or its
    single top-level directory (how zip tools pack a folder)."""
    if _load.is_pack_dir(tree):
        return tree
    names = [n for n in os.listdir(tree) if not n.startswith(".") and n != "__MACOSX"]
    if len(names) == 1 and _load.is_pack_dir(os.path.join(tree, names[0])):
        return os.path.join(tree, names[0])
    raise InstallError("no pack.yaml (or pack.json.gz) at the top of the archive")


def _safe_extract(zf: zipfile.ZipFile, dest: str):
    dest = os.path.abspath(dest)
    for info in zf.infolist():
        target = os.path.abspath(os.path.join(dest, info.filename))
        if not (target == dest or target.startswith(dest + os.sep)):
            raise InstallError("archive member %r escapes the pack folder" % info.filename)
    zf.extractall(dest)


def inspect(src: str) -> Tuple[Pack, Report, str, Optional[str]]:
    """Load and validate what ``src`` points at without installing it.
    Returns ``(pack, report, pack_dir_or_file, tempdir)``; the caller removes
    ``tempdir`` (set when ``src`` was an archive)."""
    src = os.path.abspath(os.path.expanduser(src))
    tmp = None
    if os.path.isdir(src):
        if not _load.is_pack_dir(src):
            raise InstallError("%s is not a definitions pack (no pack.yaml)" % src)
        root = src
    elif os.path.isfile(src) and zipfile.is_zipfile(src):
        tmp = tempfile.mkdtemp(prefix="ovtdiag-pack-")
        try:
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, tmp)
            root = _pack_root_in(tmp)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    elif os.path.isfile(src) and (src.endswith(".json.gz") or src.endswith(".json")):
        root = src
    else:
        raise InstallError("%s: not a pack directory, a .zip of one, or a compiled pack.json.gz" % src)
    try:
        pack = _load.load_pack(root, "user")
    except (DefsError, OSError) as ex:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError(str(ex))
    return pack, validate_pack(pack), root, tmp


def version_key(version: str) -> Tuple:
    """A sortable form of a pack version: its numeric parts as numbers
    (``1.10.0`` after ``1.9.0``), a pre-release suffix sorting before the
    bare version (``1.0.0-rc1`` before ``1.0.0``)."""
    v = str(version or "").strip()
    main, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", main) if x != "") or (0,)
    return (nums, 0 if pre else 1, pre)


def _installed_version(target: str) -> Optional[str]:
    try:
        return _load.load_pack(target, "user").version
    except Exception:
        return None


def _place(root: str, target: str):
    """Copy the pack at ``root`` (a directory, a ``pack.json.gz`` or a plain
    ``.json`` compiled pack) to ``target``; a plain JSON compiled pack is
    gzipped so the installed file is what its name says."""
    if os.path.isdir(root):
        shutil.copytree(root, target)
        return
    os.makedirs(target)
    out = os.path.join(target, _load.COMPILED_NAME)
    if root.endswith(".gz"):
        shutil.copy2(root, out)
        return
    with open(root, "rb") as src, open(out, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
            shutil.copyfileobj(src, fh)


def install_pack(src: str, dest_dir: Optional[str] = None, force: bool = False
                 ) -> Tuple[Pack, str, List[str]]:
    """Install the pack at ``src`` into ``dest_dir`` (default: the user's
    definitions folder). Returns ``(pack, installed_path, warnings)``.
    Raises InstallError when the pack does not validate, or is older than
    the installed pack of the same id (both unless ``force``), when ``src``
    is not a pack, or when the folder cannot be written (a file of the
    installed copy open elsewhere)."""
    dest_dir = dest_dir or _load.user_dir()
    pack, rep, root, tmp = inspect(src)
    try:
        if not rep.ok and not force:
            raise InstallError("pack %s does not validate:\n%s" % (pack.id, rep.text()))
        if pack.id in ("", ".", "..") or os.sep in pack.id or "/" in pack.id:
            raise InstallError("pack id %r cannot name a folder" % pack.id)
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, pack.id)
        if os.path.abspath(root) == os.path.abspath(target):
            raise InstallError("%s is already the installed copy" % target)
        if os.path.exists(target):
            have = _installed_version(target)
            if have is not None and version_key(pack.version) < version_key(have) and not force:
                raise InstallError("pack %s %s is older than the installed %s (use --force to downgrade)"
                                   % (pack.id, pack.version, have))
            prev = target + PREVIOUS_SUFFIX
            if os.path.exists(prev):
                shutil.rmtree(prev, ignore_errors=True)
            try:
                os.replace(target, prev)
            except PermissionError as ex:
                raise InstallError("cannot set aside the installed %s (a file in it is open elsewhere?): %s"
                                   % (target, ex)) from ex
        try:
            _place(root, target)
        except PermissionError as ex:
            raise InstallError("cannot write %s: %s" % (target, ex)) from ex
        return pack, target, rep.warnings
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def remove_pack(pack_id: str, dest_dir: Optional[str] = None) -> bool:
    """Remove an installed user pack by id (and its ``.previous`` copy).
    Returns whether anything was removed."""
    dest_dir = dest_dir or _load.user_dir()
    hit = False
    for name in (pack_id, pack_id + PREVIOUS_SUFFIX):
        p = os.path.join(dest_dir, name)
        if os.path.isdir(p) and _load.is_pack_dir(p):
            shutil.rmtree(p)
            hit = True
    return hit


def installed_user_packs(dest_dir: Optional[str] = None) -> List[Pack]:
    """The packs in the user's definitions folder (``.previous`` copies are
    not packs the resolver loads, and are not listed)."""
    dest_dir = dest_dir or _load.user_dir()
    if not os.path.isdir(dest_dir):
        return []
    out = []
    for n in sorted(os.listdir(dest_dir)):
        if n.endswith(PREVIOUS_SUFFIX):
            continue
        p = os.path.join(dest_dir, n)
        if _load.is_pack_dir(p):
            try:
                out.append(_load.load_pack(p, "user"))
            except (DefsError, OSError):
                continue
    return out
