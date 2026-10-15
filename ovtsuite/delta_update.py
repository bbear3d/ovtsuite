# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Delta updates: small signed packages that move an installed copy of the
app from one release to the next, chained when the user is several releases
behind. No Qt here (:mod:`ovtsuite.gui_update` is the window side); the
design is in docs/UPDATES.md.

The pieces, in the order they are used:

* **The app manifest** -- ``app-manifest.json`` in the app's folder: the
  version and the SHA-256 of every file the build put there (not ``modules\\``,
  which have their own packages and versions), signed with the release key
  (``app-manifest.json.sig``). Written by ``tools/app_release.py manifest``
  and shipped by the installer.
* **The delta package** -- ``OVTSuite-Update-<from>-to-<to>.zip``, built by
  ``tools/app_release.py delta`` from the previous release's manifest
  (archived by the build) and the new build::

      delta.json          from, to, from_files (what each touched file must be
                          before), changed, deleted, to_manifest (the whole
                          new manifest), manifest_sha256
      delta.json.sig      Ed25519 over delta.json's exact bytes (required)
      app-manifest.json   the new manifest (+ .sig), written into the app
      payload/<relpath>   every changed or added file

  Files are compared by their hash, not their bytes: PyInstaller's output is
  not byte-identical between builds, but an unchanged file hashes the same.
* **Chains** -- each release carries the delta from the release before it;
  :func:`find_chain` picks the shortest path from the installed version to
  the latest one among every published release's deltas.
* **Pre-flight** (:func:`preflight`) -- while the app is still open: every
  package is verified (signature, payload hashes), the chain is simulated
  (each step's ``from_files`` must be what the step before leaves) and the
  installed files are checked against what the first step expects. Anything
  wrong and the app falls back to the full installer instead.
* **Applying** (:func:`helper_main`) -- in a helper process started after the
  app has closed: the app's own exe, run from a temporary copy of the app
  folder (:func:`prepare_runner`) so nothing it runs from is being replaced.
  It waits for the app to exit and the app mutex to be free, verifies
  everything again, stages the new files beside the app, moves each replaced
  file into ``.update-backup`` and the new one into place, deletes what the
  release removed, writes the new manifest and the version in Apps &
  features, and on ANY failure puts every file back. It leaves a result file
  that the app reports at its next start, then starts the app again.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import branding

FORMAT = 1
MANIFEST_NAME = "app-manifest.json"
MANIFEST_SIG = MANIFEST_NAME + ".sig"
DELTA_JSON = "delta.json"
DELTA_SIG = DELTA_JSON + ".sig"
PAYLOAD = "payload/"
BACKUP_DIR = ".update-backup"
STAGE_DIR = ".update-stage"
RESULT_NAME = "update-result.json"
PLAN_NAME = "delta-plan.json"

#: ``OVTSuite.exe --apply-update <plan.json>``: the helper (packaging/entry.py)
APPLY_ARG = "--apply-update"

#: never part of the app manifest (top-level names, compared case-blind):
#: the modules have their own packages; the rest is the update's own working
#: folders and the manifest itself
SKIP_TOP = {"modules", BACKUP_DIR, STAGE_DIR, MANIFEST_NAME, MANIFEST_SIG}
#: top-level files the installer or a run leaves beside the app
_SKIP_FILE = re.compile(r"^(unins\d+\.(exe|dat|msg)|.*\.log)$", re.I)

_DELTA_RX = re.compile(r"^%s-Update-(\d+(?:\.\d+)*)-to-(\d+(?:\.\d+)*)\.zip$"
                       % re.escape(branding.EXE_NAME))

#: how long the helper waits for the app to exit / every copy to close
WAIT_SECONDS = 120


class DeltaError(Exception):
    """A delta package cannot be used, or applying it failed (and was rolled
    back). The text is for the user."""


# ------------------------------------------------------------------ names
def delta_name(from_version: str, to_version: str) -> str:
    return "%s-Update-%s-to-%s.zip" % (branding.EXE_NAME, from_version, to_version)


def parse_delta_name(name: str) -> Optional[Tuple[str, str]]:
    """``"OVTSuite-Update-0.4.8-to-0.4.9.zip"`` -> ``("0.4.8", "0.4.9")``."""
    m = _DELTA_RX.match(name or "")
    return (m.group(1), m.group(2)) if m else None


def safe_rel(rel: str) -> bool:
    """A manifest path: relative, forward slashes, no ``..``, and not one of
    the names the update never touches."""
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel:
        return False
    if rel.startswith("/"):
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if parts[0].lower() in {s.lower() for s in SKIP_TOP}:
        return False
    if len(parts) == 1 and _SKIP_FILE.match(parts[0]):
        return False
    return True


# ------------------------------------------------------------------ hashing
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(root: str) -> Dict[str, str]:
    """``{relpath: sha256}`` of every file under ``root`` that belongs in the
    app manifest."""
    out = {}
    for d, dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, root).replace(os.sep, "/")
        if rel_d == ".":
            dirs[:] = [x for x in dirs if x.lower() not in {s.lower() for s in SKIP_TOP}]
            rel_d = ""
        dirs.sort()
        for n in sorted(files):
            rel = (rel_d + "/" + n) if rel_d else n
            if safe_rel(rel):
                out[rel] = sha256_file(os.path.join(d, n))
    return out


# ------------------------------------------------------------------ the manifest
@dataclass
class AppManifest:
    version: str
    files: Dict[str, str]
    app: str = branding.APP_NAME
    format: int = FORMAT

    def to_bytes(self) -> bytes:
        return json.dumps({"app": self.app, "format": self.format,
                           "version": self.version, "files": self.files},
                          indent=0, sort_keys=True).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "AppManifest":
        try:
            d = json.loads(raw.decode("utf-8"))
            if not isinstance(d, dict) or d.get("format") != FORMAT:
                raise DeltaError("unsupported app manifest format %r"
                                 % (d.get("format") if isinstance(d, dict) else d))
            files = d["files"]
            if not isinstance(files, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
                raise DeltaError("the app manifest's file list is malformed")
            return cls(str(d["version"]), dict(files), str(d.get("app") or ""), FORMAT)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as ex:
            raise DeltaError("unreadable app manifest: %s" % ex)


def make_manifest(root: str, version: str) -> AppManifest:
    return AppManifest(version, scan_tree(root))


def write_manifest(root: str, version: str, key_path: Optional[str] = None) -> AppManifest:
    """Write ``app-manifest.json`` (and its signature with ``key_path``) into
    the built app folder ``root``. Deterministic: an unchanged build writes
    the same bytes."""
    man = make_manifest(root, version)
    raw = man.to_bytes()
    _write_atomic(os.path.join(root, MANIFEST_NAME), raw)
    sig_path = os.path.join(root, MANIFEST_SIG)
    if key_path:
        from .modules import signing
        _write_atomic(sig_path, signing.sign(raw, key_path))
    elif os.path.exists(sig_path):
        os.remove(sig_path)             # it would no longer match
    return man


def read_manifest(path: str) -> Tuple[AppManifest, bytes, Optional[bytes]]:
    """The manifest at ``path`` (a file, or a folder holding one), its exact
    bytes and its signature (None when unsigned)."""
    if os.path.isdir(path):
        path = os.path.join(path, MANIFEST_NAME)
    with open(path, "rb") as fh:
        raw = fh.read()
    sig = None
    if os.path.isfile(path + ".sig"):
        with open(path + ".sig", "rb") as fh:
            sig = fh.read()
    return AppManifest.from_bytes(raw), raw, sig


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------ building a delta
def diff(old: Dict[str, str], new: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """``(changed, deleted)``: files new or different in ``new``, files gone."""
    changed = sorted(r for r, h in new.items() if old.get(r) != h)
    deleted = sorted(r for r in old if r not in new)
    return changed, deleted


def build_delta(old_manifest: str, new_dir: str, out_dir: str,
                key_path: Optional[str]) -> Tuple[str, dict]:
    """Write ``OVTSuite-Update-<old>-to-<new>.zip`` into ``out_dir``: the
    files of the built app ``new_dir`` (which must hold its manifest) that
    differ from the release described by ``old_manifest`` (its manifest file,
    or a folder with it). Every payload file is checked against the new
    manifest as it is packed, so a stale manifest fails here. Returns
    ``(path, delta.json as a dict)``."""
    old, _raw, _sig = read_manifest(old_manifest)
    new, new_raw, new_sig = read_manifest(new_dir)
    if old.app and new.app and old.app != new.app:
        raise DeltaError("the manifests are of different apps (%s, %s)" % (old.app, new.app))
    changed, deleted = diff(old.files, new.files)
    info = {
        "format": FORMAT,
        "app": new.app or branding.APP_NAME,
        "from": old.version,
        "to": new.version,
        "from_files": {r: old.files.get(r) for r in changed + deleted},
        "changed": changed,
        "deleted": deleted,
        "to_manifest": {"version": new.version, "files": new.files},
        "manifest_sha256": hashlib.sha256(new_raw).hexdigest(),
    }
    raw = json.dumps(info, indent=0, sort_keys=True).encode("utf-8")
    sig = None
    if key_path:
        from .modules import signing
        sig = signing.sign(raw, key_path)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, delta_name(old.version, new.version))
    tmp = out + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.writestr(DELTA_JSON, raw)
            if sig is not None:
                z.writestr(DELTA_SIG, sig)
            z.writestr(MANIFEST_NAME, new_raw)
            if new_sig is not None:
                z.writestr(MANIFEST_SIG, new_sig)
            for rel in changed:
                src = os.path.join(new_dir, *rel.split("/"))
                if sha256_file(src) != new.files[rel]:
                    raise DeltaError("%s does not match the build's manifest -- "
                                     "write the manifest again" % rel)
                z.write(src, PAYLOAD + rel)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out, info


# ------------------------------------------------------------------ reading a delta
@dataclass
class Delta:
    path: str
    from_version: str
    to_version: str
    from_files: Dict[str, Optional[str]]
    changed: List[str]
    deleted: List[str]
    to_files: Dict[str, str]
    manifest_bytes: bytes
    manifest_sig: Optional[bytes]
    signed_by: str = ""

    def read(self, rel: str) -> bytes:
        with zipfile.ZipFile(self.path) as z:
            return z.read(PAYLOAD + rel)


def base_keys() -> Dict[str, str]:
    """The release keys the app trusts: the base app's own
    ``module_keys.json``, read directly -- not through the data-file lookup,
    which would load the module registry (the helper needs none of it)."""
    from .datafiles import BASE_DIR
    from .modules import signing
    try:
        with open(os.path.join(BASE_DIR, signing.KEYS_FILE), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k["id"]: k["public"] for k in raw.get("keys", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _verify_sig(raw: bytes, sig: Optional[bytes], keys) -> Tuple[bool, str]:
    from .modules import signing
    return signing.verify(raw, sig, base_keys() if keys is None else keys)


def open_delta(path: str, keys=None) -> Delta:
    """Open and check a delta package: its signature (a trusted key is
    required -- there is no unsigned fallback), its structure and that it
    agrees with itself. The payload's hashes are checked by
    :func:`check_payload`."""
    name = os.path.basename(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as ex:
        raise DeltaError("%s is not an update package: %s" % (name, ex))
    with z:
        names = set(z.namelist())
        if DELTA_JSON not in names:
            raise DeltaError("%s is not an update package (no %s)" % (name, DELTA_JSON))
        raw = z.read(DELTA_JSON)
        sig = z.read(DELTA_SIG) if DELTA_SIG in names else None
        ok, who = _verify_sig(raw, sig, keys)
        if not ok:
            raise DeltaError("%s is %s" % (name, "not signed" if who == "unsigned"
                                           else "not verifiable (%s)" % who))
        try:
            d = json.loads(raw.decode("utf-8"))
            if d.get("format") != FORMAT:
                raise DeltaError("%s has an unsupported format (%r)" % (name, d.get("format")))
            if d.get("app") != branding.APP_NAME:
                raise DeltaError("%s is for %s" % (name, d.get("app")))
            frm, to = str(d["from"]), str(d["to"])
            from_files = dict(d["from_files"])
            changed, deleted = list(d["changed"]), list(d["deleted"])
            tm = d["to_manifest"]
            to_files = dict(tm["files"])
            if str(tm["version"]) != to:
                raise DeltaError("%s: its manifest is of another version" % name)
            msha = str(d["manifest_sha256"])
        except (ValueError, KeyError, TypeError, AttributeError) as ex:
            raise DeltaError("%s: unreadable %s (%s)" % (name, DELTA_JSON, ex))
        rels = list(from_files) + changed + deleted + list(to_files)
        if not all(safe_rel(r) for r in rels):
            raise DeltaError("%s names a file outside the app" % name)
        if not all(isinstance(v, str) for v in to_files.values()) or not all(
                v is None or isinstance(v, str) for v in from_files.values()):
            raise DeltaError("%s: malformed file list" % name)
        ch, dl = set(changed), set(deleted)
        if (ch & dl or not ch <= set(to_files) or dl & set(to_files)
                or set(from_files) != ch | dl
                or any(from_files[r] is None for r in dl)):
            raise DeltaError("%s does not agree with itself" % name)
        payload = {n for n in names if n.startswith(PAYLOAD) and not n.endswith("/")}
        if payload != {PAYLOAD + r for r in changed}:
            raise DeltaError("%s: its contents do not match its file list" % name)
        if MANIFEST_NAME not in names:
            raise DeltaError("%s carries no app manifest" % name)
        mraw = z.read(MANIFEST_NAME)
        msig = z.read(MANIFEST_SIG) if MANIFEST_SIG in names else None
    if hashlib.sha256(mraw).hexdigest() != msha:
        raise DeltaError("%s: its app manifest has been changed" % name)
    man = AppManifest.from_bytes(mraw)
    if man.version != to or man.files != to_files:
        raise DeltaError("%s: its app manifest disagrees with %s" % (name, DELTA_JSON))
    return Delta(path, frm, to, from_files, changed, deleted, to_files, mraw, msig, who)


def check_payload(delta: Delta) -> None:
    """Every payload file hashes to what the new manifest says."""
    with zipfile.ZipFile(delta.path) as z:
        for rel in delta.changed:
            h = hashlib.sha256()
            with z.open(PAYLOAD + rel) as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != delta.to_files[rel]:
                raise DeltaError("%s: %s is damaged" % (os.path.basename(delta.path), rel))


# ------------------------------------------------------------------ chains
@dataclass
class Composite:
    """A chain of deltas folded into one change of the app folder."""
    deltas: List[Delta]
    writes: Dict[str, int]                  # relpath -> index of the delta with its content
    deletes: List[str]
    expect: Dict[str, Optional[str]]        # what each touched file must be now
    final_files: Dict[str, str]

    @property
    def start(self) -> str:
        return self.deltas[0].from_version

    @property
    def end(self) -> str:
        return self.deltas[-1].to_version

    @property
    def touched(self) -> Set[str]:
        return set(self.writes) | set(self.deletes)


def _same_version(a: str, b: str) -> bool:
    from .updates import compare_versions
    try:
        return compare_versions(a, b) == 0
    except ValueError:
        return a == b


def compose(deltas: List[Delta], current: str) -> Composite:
    """Fold the chain ``deltas`` (in order) into one change, checking that it
    starts at ``current`` and that every step finds what the steps before it
    leave."""
    if not deltas:
        raise DeltaError("no update packages")
    if not _same_version(deltas[0].from_version, current):
        raise DeltaError("the update starts at %s, this is %s"
                         % (deltas[0].from_version, current))
    state: Dict[str, Optional[str]] = {}
    expect: Dict[str, Optional[str]] = {}
    writes: Dict[str, int] = {}
    deletes: Set[str] = set()
    for i, d in enumerate(deltas):
        if i and not _same_version(d.from_version, deltas[i - 1].to_version):
            raise DeltaError("the update packages do not follow on (%s, then %s)"
                             % (deltas[i - 1].to_version, d.from_version))
        for rel, want in d.from_files.items():
            if rel in state:
                have = state[rel]
            elif i:
                have = deltas[i - 1].to_files.get(rel)
            else:
                expect[rel] = want
                continue
            if have != want:
                raise DeltaError("the update from %s to %s expects a %s that the "
                                 "step before it does not leave" % (d.from_version,
                                                                    d.to_version, rel))
            if rel not in state:
                expect.setdefault(rel, want)
        for rel in d.changed:
            state[rel] = d.to_files[rel]
            writes[rel] = i
            deletes.discard(rel)
        for rel in d.deleted:
            state[rel] = None
            writes.pop(rel, None)
            deletes.add(rel)
    final = deltas[-1].to_files
    for rel, i in writes.items():
        if final.get(rel) != deltas[i].to_files[rel]:
            raise DeltaError("the last update package does not list %s as the "
                             "step before left it" % rel)
    if any(rel in final for rel in deletes):
        raise DeltaError("the update packages disagree about a deleted file")
    return Composite(list(deltas), writes, sorted(deletes), expect, dict(final))


def check_disk(app_dir: str, comp: Composite) -> List[str]:
    """What is not as the first step expects (empty: all is well). A file the
    update adds may already be there (it is replaced)."""
    bad = []
    for rel, want in sorted(comp.expect.items()):
        if want is None:
            continue
        p = os.path.join(app_dir, *rel.split("/"))
        if not os.path.isfile(p):
            bad.append("%s is missing" % rel)
        elif sha256_file(p) != want:
            bad.append("%s is not the one %s installed" % (rel, comp.start))
    return bad


def preflight(app_dir: str, paths: List[str], current: str, keys=None) -> Composite:
    """Everything that can be checked before the app closes: each package's
    signature, structure and payload, the chain, and the installed files.
    Raises :class:`DeltaError` (fall back to the full installer)."""
    deltas = [open_delta(p, keys) for p in paths]
    for d in deltas:
        check_payload(d)
    comp = compose(deltas, current)
    bad = check_disk(app_dir, comp)
    if bad:
        raise DeltaError("%d installed file%s differ%s from %s %s: %s" % (
            len(bad), "" if len(bad) == 1 else "s", "s" if len(bad) == 1 else "",
            branding.APP_NAME, comp.start, "; ".join(bad[:3])))
    return comp


# ------------------------------------------------------------------ choosing
@dataclass
class Hop:
    release: object                     # updates.Release
    asset: object                       # updates.Asset
    from_version: str
    to_version: str


@dataclass
class Chain:
    hops: List[Hop] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(int(getattr(h.asset, "size", 0) or 0) for h in self.hops)

    @property
    def steps(self) -> int:
        return len(self.hops)


def _vkey(v: str):
    from .updates import _pad, parse_version
    nums, pre = parse_version(v)
    return _pad(nums, 8), pre


def delta_hops(releases) -> List[Hop]:
    """Every usable delta among ``releases``: one published with the release
    it updates TO (``OVTSuite-Update-<a>-to-<b>.zip`` in release ``b``)."""
    out = []
    for rel in releases:
        if not getattr(rel, "usable", False):
            continue
        for a in rel.assets:
            ft = parse_delta_name(a.name)
            if ft and _same_version(ft[1], rel.version):
                try:
                    if _vkey(ft[0]) < _vkey(ft[1]):
                        out.append(Hop(rel, a, ft[0], ft[1]))
                except ValueError:
                    continue
    return out


def find_chain(releases, current: str, target: str) -> Optional[Chain]:
    """The shortest chain of deltas from ``current`` to ``target`` (fewest
    steps, then fewest bytes), or None."""
    import heapq
    try:
        start, goal = _vkey(current), _vkey(target)
    except ValueError:
        return None
    edges: Dict[tuple, List[Hop]] = {}
    for h in delta_hops(releases):
        try:
            a, b = _vkey(h.from_version), _vkey(h.to_version)
        except ValueError:
            continue
        if start <= a and b <= goal:
            edges.setdefault(a, []).append(h)
    queue = [(0, 0, 0, start, [])]
    best: Dict[tuple, Tuple[int, int]] = {}
    n = 0
    while queue:
        steps, size, _n, node, path = heapq.heappop(queue)
        if node == goal:
            return Chain(path) if path else None
        if node in best and best[node] <= (steps, size):
            continue
        best[node] = (steps, size)
        for h in edges.get(node, ()):
            n += 1
            heapq.heappush(queue, (steps + 1, size + int(h.asset.size or 0), n,
                                   _vkey(h.to_version), path + [h]))
    return None


def choose(chain: Optional[Chain], installer_size: int, per_user: bool,
           failed_version: str = "", target: str = "") -> Optional[Chain]:
    """The chain to install with, or None for the full installer: only for a
    per-user install (an all-users one needs the elevated installer), not
    for a release whose delta update failed here before, and only while the
    packages are smaller than the installer."""
    if chain is None or not chain.hops or not per_user:
        return None
    if failed_version and target and _same_version(failed_version, target):
        return None
    if installer_size and chain.size >= installer_size:
        return None
    return chain


# ------------------------------------------------------------------ applying
def _retry(fn, *args, tries: int = 8, delay: float = 0.25):
    """Run a file operation, retrying a moment later when Windows says the
    file is in use (an antivirus scan, the indexer)."""
    for i in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


def _p(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def _stage(app_dir: str, comp: Composite, stage: str) -> None:
    """Extract every file the update writes into ``stage`` (beside the app,
    so moving them in is a rename), checking each against the manifest."""
    by_delta: Dict[int, List[str]] = {}
    for rel, i in comp.writes.items():
        by_delta.setdefault(i, []).append(rel)
    for i, rels in sorted(by_delta.items()):
        d = comp.deltas[i]
        with zipfile.ZipFile(d.path) as z:
            for rel in sorted(rels):
                dst = _p(stage, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                h = hashlib.sha256()
                with z.open(PAYLOAD + rel) as src, open(dst, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != comp.final_files[rel]:
                    raise DeltaError("%s is damaged in %s" % (rel, os.path.basename(d.path)))
    last = comp.deltas[-1]
    os.makedirs(stage, exist_ok=True)
    for name, data in ((MANIFEST_NAME, last.manifest_bytes), (MANIFEST_SIG, last.manifest_sig)):
        if data is not None:
            with open(os.path.join(stage, name), "wb") as fh:
                fh.write(data)


def _remove_empty_dirs(app_dir: str, rels: Iterable[str]) -> None:
    root = os.path.normcase(os.path.abspath(app_dir))
    for rel in rels:
        d = os.path.dirname(_p(app_dir, rel))
        while os.path.normcase(os.path.abspath(d)) != root:
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)


def apply(app_dir: str, comp: Composite, log: Callable[[str], None] = lambda s: None,
          registry: Optional[Callable[[str], Optional[Callable[[], None]]]] = None) -> None:
    """Change the app folder by ``comp``. All or nothing: on any failure
    every file is put back as it was and :class:`DeltaError` says why.
    ``registry(version)`` updates the version Windows shows (it may return an
    undo for a failure after it)."""
    stage = os.path.join(app_dir, STAGE_DIR)
    backup = os.path.join(app_dir, BACKUP_DIR)
    for d in (stage, backup):
        if os.path.exists(d):
            shutil.rmtree(d)
    bad = check_disk(app_dir, comp)
    if bad:
        raise DeltaError("the installed files changed: %s" % "; ".join(bad[:3]))
    try:
        log("staging %d files" % len(comp.writes))
        _stage(app_dir, comp, stage)
    except BaseException as ex:
        shutil.rmtree(stage, ignore_errors=True)
        if isinstance(ex, (OSError, zipfile.BadZipFile, KeyError)):
            raise DeltaError("the update could not be unpacked (%s); nothing was changed"
                             % ex)
        raise
    order = sorted(comp.writes) + list(comp.deletes) + [MANIFEST_NAME, MANIFEST_SIG]
    journal: List[list] = []                 # [rel, old one backed up, new one placed]
    undo_registry = None
    try:
        for rel in order:
            dst = _p(app_dir, rel)
            new = _p(stage, rel)
            entry = [rel, False, False]
            journal.append(entry)
            if os.path.lexists(dst):
                b = _p(backup, rel)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                _retry(os.replace, dst, b)
                entry[1] = True
            if os.path.exists(new):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _retry(os.replace, new, dst)
                entry[2] = True
        for rel in comp.writes:
            if sha256_file(_p(app_dir, rel)) != comp.final_files[rel]:
                raise DeltaError("%s is not right after it was written" % rel)
        if registry is not None:
            undo_registry = registry(comp.end)
        log("files in place: %d written, %d deleted" % (len(comp.writes), len(comp.deletes)))
    except BaseException as ex:
        log("FAILED: %s -- rolling back" % ex)
        problems = _rollback(app_dir, backup, journal, log)
        if undo_registry is not None:
            try:
                undo_registry()
            except Exception as uex:            # noqa: BLE001
                problems.append("registry: %s" % uex)
        shutil.rmtree(stage, ignore_errors=True)
        why = str(ex) if isinstance(ex, DeltaError) else "%s: %s" % (type(ex).__name__, ex)
        if problems:
            raise DeltaError("the update failed (%s) and %d file%s could not be put "
                             "back (%s); reinstall %s with its setup program"
                             % (why, len(problems), "" if len(problems) == 1 else "s",
                                "; ".join(problems[:3]), branding.APP_NAME))
        raise DeltaError("the update failed and %s %s was restored: %s"
                         % (branding.APP_NAME, comp.start, why))
    _remove_empty_dirs(app_dir, comp.deletes)
    shutil.rmtree(stage, ignore_errors=True)
    # the backup may hold a file still mapped by this helper: what is left
    # goes at the app's next start (cleanup_after_update)
    shutil.rmtree(backup, ignore_errors=True)


def _rollback(app_dir: str, backup: str, journal: List[list],
              log: Callable[[str], None]) -> List[str]:
    problems = []
    for rel, backed, placed in reversed(journal):
        dst = _p(app_dir, rel)
        try:
            if placed and os.path.lexists(dst):
                _retry(os.remove, dst)
            if backed:
                _retry(os.replace, _p(backup, rel), dst)
        except OSError as ex:
            problems.append("%s: %s" % (rel, ex))
            log("rollback: %s: %s" % (rel, ex))
    return problems


# ------------------------------------------------------------------ Windows
def update_uninstall_entry(app_dir: str, version: str, winreg=None,
                           app_id: Optional[str] = None) -> Optional[Callable[[], None]]:
    """Make Apps & features show ``version``: the Inno Setup uninstall entry
    whose InstallLocation is ``app_dir`` (per user first, then machine-wide).
    Returns an undo, or None when there is no entry to change."""
    if winreg is None:
        try:
            import winreg               # noqa: F811
        except ImportError:
            return None
    if app_id is None:
        from .updates import INNO_APP_ID as app_id
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % app_id
    want = os.path.normcase(os.path.abspath(app_dir)).rstrip("\\/")
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            k = winreg.OpenKey(root, key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except OSError:
            continue
        with k:
            try:
                loc = winreg.QueryValueEx(k, "InstallLocation")[0]
            except OSError:
                continue
            if os.path.normcase(os.path.abspath(loc)).rstrip("\\/") != want:
                continue
            new = {"DisplayVersion": (version, winreg.REG_SZ),
                   "DisplayName": ("%s %s" % (branding.APP_NAME, version), winreg.REG_SZ)}
            try:
                from .updates import parse_version
                nums = parse_version(version)[0]
                new["VersionMajor"] = (nums[0], winreg.REG_DWORD)
                new["VersionMinor"] = (nums[1] if len(nums) > 1 else 0, winreg.REG_DWORD)
            except ValueError:
                pass
            old = {}
            for name in new:
                try:
                    old[name] = winreg.QueryValueEx(k, name)
                except OSError:
                    old[name] = None
            # only the version numbers Inno wrote are touched
            for name in ("VersionMajor", "VersionMinor"):
                if old.get(name) is None:
                    new.pop(name, None)
            done = []

            def undo(root=root, done=done, old=old):
                with winreg.OpenKey(root, key, 0, winreg.KEY_SET_VALUE) as uk:
                    for nm in done:
                        if old[nm] is None:
                            winreg.DeleteValue(uk, nm)
                        else:
                            winreg.SetValueEx(uk, nm, 0, old[nm][1], old[nm][0])
            try:
                for name, (val, typ) in new.items():
                    winreg.SetValueEx(k, name, 0, typ, val)
                    done.append(name)
            except OSError:
                undo()
                raise
            return undo
    return None


_SYNCHRONIZE = 0x00100000


def wait_for_exit(pid: int, timeout: float = WAIT_SECONDS) -> bool:
    """True once process ``pid`` has exited (False: still running)."""
    if not pid:
        return True
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if not h:
            return True                 # gone already
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(h), int(timeout * 1000)) == 0
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def wait_mutex_free(name: str = branding.APP_MUTEX, timeout: float = WAIT_SECONDS) -> bool:
    """Windows: True once nothing holds the app mutex (every copy of the app
    and its tools has closed). Elsewhere always True."""
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenMutexW.restype = ctypes.c_void_p
    end = time.time() + timeout
    while True:
        h = k32.OpenMutexW(_SYNCHRONIZE, False, name)
        if not h:
            return True
        k32.CloseHandle(ctypes.c_void_p(h))
        if time.time() >= end:
            return False
        time.sleep(0.5)


def _detached(cmd: List[str], cwd: str) -> None:
    import subprocess
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(cmd, cwd=cwd, close_fds=True, creationflags=flags
                         | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        # a job that does not allow breaking away
        subprocess.Popen(cmd, cwd=cwd, close_fds=True, creationflags=flags)


# ------------------------------------------------------------------ the helper
def runner_root() -> str:
    return tempfile.gettempdir()


RUNNER_PREFIX = branding.EXE_NAME + "-apply-"


def prepare_runner(app_dir: str, dest: str, copy: Iterable[str] = ()) -> None:
    """Lay out a copy of the app folder at ``dest`` to run the helper from:
    hard links where the volume allows (instant, no space), real copies for
    the files the update replaces or deletes (``copy``) -- so the helper
    never runs from a file that is being moved -- and everywhere a link
    cannot be made. The modules and the update's own folders are left out."""
    copy_set = {os.path.normcase(r) for r in copy}
    if os.path.exists(dest):
        shutil.rmtree(dest)
    for d, dirs, files in os.walk(app_dir):
        rel_d = os.path.relpath(d, app_dir).replace(os.sep, "/")
        rel_d = "" if rel_d == "." else rel_d
        if not rel_d:
            dirs[:] = [x for x in dirs if x.lower() not in {s.lower() for s in SKIP_TOP}]
        os.makedirs(os.path.join(dest, *rel_d.split("/")) if rel_d else dest, exist_ok=True)
        for n in files:
            rel = (rel_d + "/" + n) if rel_d else n
            if not rel_d and (_SKIP_FILE.match(n) or n in (MANIFEST_NAME, MANIFEST_SIG)):
                continue
            src, dst = os.path.join(d, n), _p(dest, rel)
            if os.path.normcase(rel) not in copy_set:
                try:
                    os.link(src, dst)
                    continue
                except OSError:
                    pass
            shutil.copy2(src, dst)


def result_path() -> str:
    """Where the helper leaves its result for the app's next start."""
    try:
        from .recent import _config_dir     # beside the diagnostics log
        return os.path.join(_config_dir(), RESULT_NAME)
    except Exception:                       # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), "." + branding.PKG + "-" + RESULT_NAME)


def write_plan(folder: str, app_dir: str, relaunch: str, pid: int, deltas: List[str],
               from_version: str, to_version: str, then: str = "",
               result: Optional[str] = None) -> str:
    """The helper's instructions, beside the downloads."""
    plan = {"format": FORMAT, "app_dir": os.path.abspath(app_dir),
            "relaunch": os.path.abspath(relaunch), "pid": int(pid),
            "deltas": [os.path.abspath(p) for p in deltas],
            "from": from_version, "to": to_version, "then": then or "",
            "result": result or result_path(),
            "log": os.path.join(os.path.abspath(folder), "delta-update.log")}
    path = os.path.join(folder, PLAN_NAME)
    _write_atomic(path, json.dumps(plan, indent=1).encode("utf-8"))
    return path


def prepare(app_dir: str, comp: Composite, folder: str, then: str = "",
            relaunch: Optional[str] = None) -> List[str]:
    """Write the helper's plan and lay out its copy of the app; returns the
    command line that starts it (:func:`launch`). Raises OSError.

    Why a copy of the app's own exe: the helper needs Python, the signature
    check and this module, which the app folder already has -- but it must
    not run from the files it replaces. Hard links make the copy instant;
    the files the update touches are copied for real."""
    exe = branding.EXE_NAME + ".exe"
    os.makedirs(folder, exist_ok=True)
    if not relaunch:
        from .launcher import relaunch_exe  # a tool comes back as the tool
        relaunch = relaunch_exe()
    plan = write_plan(folder, app_dir, relaunch, os.getpid(),
                      [d.path for d in comp.deltas], comp.start, comp.end, then)
    runner = os.path.join(runner_root(), RUNNER_PREFIX + comp.end)
    prepare_runner(app_dir, runner, comp.touched | {exe})
    return [os.path.join(runner, exe), APPLY_ARG, plan]


def launch(cmd: List[str]) -> None:
    """Start the helper :func:`prepare` made; it waits for this process to
    exit. Raises OSError."""
    _detached(cmd, os.path.dirname(cmd[0]))


def write_result(path: str, result: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _write_atomic(path, json.dumps(result, indent=1).encode("utf-8"))
    except OSError:
        pass


def pop_result(path: Optional[str] = None) -> Optional[dict]:
    """The last delta update's result, once: the file is removed."""
    path = path or result_path()
    try:
        with open(path, "rb") as fh:
            r = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    return r if isinstance(r, dict) else None


def cleanup_after_update(app_dir: str) -> None:
    """At start-up: what a finished update left (its backup, a stage, the
    helper's copy of the app)."""
    for n in (BACKUP_DIR, STAGE_DIR):
        shutil.rmtree(os.path.join(app_dir, n), ignore_errors=True)
    base = runner_root()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for n in names:
        if n.startswith(RUNNER_PREFIX):
            shutil.rmtree(os.path.join(base, n), ignore_errors=True)


def run_plan(plan: dict, log: Callable[[str], None], keys=None,
             registry=None, wait: bool = True) -> dict:
    """Carry out a plan (the helper's work, minus starting anything): returns
    the result written for the app."""
    result = {"ok": False, "from": plan.get("from", ""), "to": plan.get("to", ""),
              "message": "", "time": time.time(), "log": plan.get("log", "")}
    try:
        app_dir = plan["app_dir"]
        if wait:
            log("waiting for process %s to exit" % plan.get("pid"))
            if not wait_for_exit(plan.get("pid", 0)):
                raise DeltaError("%s did not close" % branding.APP_NAME)
            if not wait_mutex_free():
                raise DeltaError("other copies of %s or its tools are still running"
                                 % branding.APP_NAME)
        comp = preflight(app_dir, plan["deltas"], plan["from"], keys)
        if not _same_version(comp.end, plan["to"]):
            raise DeltaError("the update packages end at %s, not %s" % (comp.end, plan["to"]))
        log("applying %s -> %s (%d step%s)" % (comp.start, comp.end, len(comp.deltas),
                                              "" if len(comp.deltas) == 1 else "s"))
        if registry is None:
            def registry(v):
                return update_uninstall_entry(app_dir, v)
        apply(app_dir, comp, log, registry)
        result.update(ok=True, message="%s was updated from %s to %s"
                      % (branding.APP_NAME, comp.start, comp.end))
    except DeltaError as ex:
        result["message"] = str(ex)
    except Exception as ex:                 # noqa: BLE001 - reported, never fatal
        import traceback
        log(traceback.format_exc())
        result["message"] = "unexpected error: %s" % ex
    log(("OK: " if result["ok"] else "FAILED: ") + result["message"])
    return result


def helper_main(argv: List[str]) -> int:
    """``OVTSuite.exe --apply-update <plan.json>`` (packaging/entry.py), run
    from the temporary copy :func:`start` made."""
    if not argv:
        return 2
    try:
        with open(argv[0], "rb") as fh:
            plan = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return 2
    logf = None
    try:
        logf = open(plan.get("log") or os.devnull, "a", encoding="utf-8")
    except OSError:
        pass

    def log(msg):
        if logf is not None:
            logf.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
            logf.flush()
    result = run_plan(plan, log)
    write_result(plan.get("result") or result_path(), result)
    try:
        then = plan.get("then") or ""
        if then:
            # the module installers' batch file; it starts the app at its end
            cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                               "System32", "cmd.exe")
            _detached([cmd, "/d", "/c", then], os.path.dirname(then))
        elif plan.get("relaunch"):
            _detached([plan["relaunch"]], plan.get("app_dir") or None)
    except OSError as ex:
        log("could not start the app again: %s" % ex)
    if logf is not None:
        logf.close()
    return 0 if result["ok"] else 1
