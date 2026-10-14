# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Checking for a newer release, and fetching it safely. No Qt here
(:mod:`ovtsuite.gui_update` is the window side).

A release is the GitHub release ``v<version>`` of :data:`branding.SOURCE_URL`;
its assets carry the base app's installers, every module's ``.ovtmod`` and
setup.exe, and (from 0.4.7 on) ``SHA256SUMS.txt`` with its detached Ed25519
signature ``SHA256SUMS.txt.sig``, made by ``tools/build_modules.py
release-sums`` with the same key that signs the modules (so the app trusts it
through ``module_keys.json``).

What is offered: the base app when the release is newer than this one, and
every installed module whose package in the release is newer than the
installed copy. Prereleases and drafts are never offered.

A download is kept only when its size equals the asset's and its SHA-256
equals the signed checksum list's -- or, without a list, GitHub's own
``digest`` for the asset; a file neither vouches for is reported as
unverified and the user decides. A ``.ovtmod`` is checked again by the module
installer (signature and every file).
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import branding

#: QSettings keys (not user options; the option is ``updates/check_at_startup``)
LAST_CHECK_KEY = "updates/last_check"
SKIP_KEY = "updates/skip"
OPTION_KEY = "updates/check_at_startup"

#: the automatic check runs at most this often (seconds)
CHECK_INTERVAL = 24 * 3600

SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = SUMS_NAME + ".sig"

#: Windows: what the running app holds while it runs (packaging/entry.py), and
#: what the installers' AppMutex waits for. = AppMutex in packaging/tools.iss.
APP_MUTEX = branding.APP_MUTEX

#: Inno Setup switches for an unattended update. /CLOSEAPPLICATIONS lets the
#: Restart Manager close anything still holding a file; the app itself has
#: already closed by then.
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")
RELAUNCH_ARG = "/RELAUNCH=1"


class UpdateError(Exception):
    """A check or a download failed; the text is for the user."""


# ------------------------------------------------------------------ versions
_VER = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$", re.I)


def parse_version(text: str) -> Tuple[Tuple[int, ...], bool]:
    """``"v0.4.10"`` -> ``((0, 4, 10), False)``; the flag is True for a
    prerelease (``0.5.0-rc1``, ``0.5.0b2``, ``1.0.dev3``). Raises ValueError
    for something that is not a version."""
    m = _VER.match((text or "").strip())
    if not m:
        raise ValueError("not a version: %r" % text)
    nums = tuple(int(x) for x in m.group(1).split("."))
    rest = m.group(2).strip()
    # "+build" metadata is not a prerelease; anything else after the numbers is
    pre = bool(rest) and not rest.startswith("+")
    return nums, pre


def _pad(t: Tuple[int, ...], n: int) -> Tuple[int, ...]:
    return t + (0,) * (n - len(t))


def compare_versions(a: str, b: str) -> int:
    """-1, 0 or 1 as version ``a`` is older than, equal to or newer than
    ``b``, numerically (1.0.10 is newer than 1.0.9). A prerelease of a
    number is older than the release of the same number."""
    (na, pa), (nb, pb) = parse_version(a), parse_version(b)
    n = max(len(na), len(nb))
    na, nb = _pad(na, n), _pad(nb, n)
    if na != nb:
        return 1 if na > nb else -1
    if pa != pb:
        return -1 if pa else 1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    try:
        return compare_versions(candidate, current) > 0
    except ValueError:
        return False


# ------------------------------------------------------------------ release
def repo_slug(url: str = branding.SOURCE_URL) -> Tuple[str, str]:
    """``("owner", "repo")`` of a GitHub URL."""
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$",
                 (url or "").strip())
    if not m:
        raise ValueError("not a GitHub repository URL: %r" % url)
    return m.group(1), m.group(2)


def latest_url(url: str = branding.SOURCE_URL) -> str:
    return "https://api.github.com/repos/%s/%s/releases/latest" % repo_slug(url)


def releases_page(url: str = branding.SOURCE_URL) -> str:
    return "https://github.com/%s/%s/releases" % repo_slug(url)


@dataclass
class Asset:
    name: str
    size: int
    url: str                       # browser_download_url
    digest: str = ""               # GitHub's "sha256:<hex>", when it gives one

    @property
    def sha256(self) -> str:
        d = (self.digest or "").strip().lower()
        return d[len("sha256:"):] if d.startswith("sha256:") else ""


@dataclass
class Release:
    tag: str
    version: str
    name: str
    body: str
    html_url: str
    assets: List[Asset] = field(default_factory=list)
    prerelease: bool = False
    draft: bool = False

    @classmethod
    def from_json(cls, j: dict) -> "Release":
        if not isinstance(j, dict) or not j.get("tag_name"):
            raise UpdateError("GitHub answered with something that is not a release")
        tag = str(j["tag_name"])
        ver = tag[1:] if tag[:1] in "vV" else tag
        assets = []
        for a in j.get("assets") or []:
            try:
                assets.append(Asset(str(a["name"]), int(a.get("size") or 0),
                                    str(a.get("browser_download_url") or ""),
                                    str(a.get("digest") or "")))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(tag, ver, str(j.get("name") or tag), str(j.get("body") or ""),
                   str(j.get("html_url") or releases_page()), assets,
                   bool(j.get("prerelease")), bool(j.get("draft")))

    def asset(self, name: str) -> Optional[Asset]:
        for a in self.assets:
            if a.name == name:
                return a
        return None

    @property
    def usable(self) -> bool:
        """Not a draft, not a prerelease, and a version we can compare."""
        if self.draft or self.prerelease:
            return False
        try:
            return not parse_version(self.version)[1]
        except ValueError:
            return False


def _urlopen(url: str, timeout: float, accept: str):
    from .ai.providers.base import _ssl_context, user_agent
    req = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": user_agent(),
        "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def fetch_latest(timeout: float = 15.0, url: Optional[str] = None) -> Release:
    """The latest published release (GitHub's ``/releases/latest`` leaves out
    drafts and prereleases). Raises :class:`UpdateError`."""
    url = url or latest_url()
    try:
        with _urlopen(url, timeout, "application/vnd.github+json") as resp:
            raw = resp.read()
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            raise UpdateError("no release has been published yet")
        if ex.code in (403, 429):
            raise UpdateError("GitHub refused the request for now (rate limit); "
                              "try again later")
        raise UpdateError("GitHub answered HTTP %d" % ex.code)
    except (urllib.error.URLError, OSError) as ex:
        raise UpdateError("could not reach GitHub: %s" % getattr(ex, "reason", ex))
    try:
        return Release.from_json(json.loads(raw.decode("utf-8")))
    except ValueError:
        raise UpdateError("GitHub answered with something that is not JSON")


def fetch_text(url: str, timeout: float = 30.0, limit: int = 1 << 20) -> bytes:
    """A small asset (the checksum list, its signature)."""
    try:
        with _urlopen(url, timeout, "application/octet-stream") as resp:
            data = resp.read(limit + 1)
    except (urllib.error.URLError, OSError) as ex:
        raise UpdateError("could not download %s: %s"
                          % (url.rsplit("/", 1)[-1], getattr(ex, "reason", ex)))
    if len(data) > limit:
        raise UpdateError("%s is unexpectedly large" % url.rsplit("/", 1)[-1])
    return data


# ------------------------------------------------------------------ what to offer
def base_names(version: str) -> List[str]:
    """The base app's Windows installers, in order of preference: base-only,
    then the full one (which also re-installs the modules it carries)."""
    e = branding.EXE_NAME
    return ["%s-Base-%s-setup.exe" % (e, version), "%s-%s-setup.exe" % (e, version)]


def base_asset(rel: Release) -> Optional[Asset]:
    for n in base_names(rel.version):
        a = rel.asset(n)
        if a is not None:
            return a
    return None


def module_setup_stem(module_name: str) -> str:
    """``"Bosch EDC16"`` -> ``"BoschEDC16"`` (build.ps1's installer names)."""
    return re.sub(r"[^A-Za-z0-9]", "", module_name)


@dataclass
class InstalledModule:
    id: str
    name: str
    version: str
    variant: str = "full"           # "full" or "checksum"
    root: str = ""                  # its folder ("" = in the source tree)
    by_setup: bool = False          # put there by its own setup.exe


@dataclass
class ModuleUpdate:
    mod: InstalledModule
    version: str
    package: Optional[Asset] = None     # <id>-<ver>[-checksum].ovtmod
    setup: Optional[Asset] = None       # OVTSuite-<Name>[-Checksum]-<ver>-setup.exe

    @property
    def method(self) -> str:
        """How it is installed on Windows: ``"package"`` (in the app, before
        it closes) or ``"setup"`` (its installer, after the app's), or ""."""
        if self.mod.by_setup and self.setup is not None:
            # in-app installing would replace the folder its uninstaller is in
            return "setup"
        if self.package is not None:
            return "package"
        return "setup" if self.setup is not None else ""


def module_updates(rel: Release, installed: List[InstalledModule]) -> List[ModuleUpdate]:
    """Every installed module with a newer version among the release's
    assets (the same variant as installed)."""
    out = []
    for m in installed:
        cks = m.variant == "checksum"
        pk = re.compile(r"^%s-(\d+(?:\.\d+)*)%s\.ovtmod$"
                        % (re.escape(m.id), "-checksum" if cks else ""))
        st = re.compile(r"^%s-%s%s-(\d+(?:\.\d+)*)-setup\.exe$"
                        % (re.escape(branding.EXE_NAME), re.escape(module_setup_stem(m.name)),
                           "-Checksum" if cks else ""))
        best: Dict[str, ModuleUpdate] = {}
        for a in rel.assets:
            for rx, attr in ((pk, "package"), (st, "setup")):
                mm = rx.match(a.name)
                if mm and is_newer(mm.group(1), m.version):
                    u = best.setdefault(mm.group(1), ModuleUpdate(m, mm.group(1)))
                    setattr(u, attr, a)
        if best:
            top = max(best, key=lambda v: _pad(parse_version(v)[0], 8))
            out.append(best[top])
    return out


@dataclass
class Offer:
    release: Release
    current: str
    base: bool                          # the app itself is newer
    modules: List[ModuleUpdate]

    @property
    def any(self) -> bool:
        return self.base or bool(self.modules)

    @property
    def key(self) -> str:
        """What "Skip this version" remembers: the release and the module
        versions in it, so a later release (or a module added to this one)
        is offered again."""
        return "|".join([self.release.tag] + sorted(
            "%s=%s" % (u.mod.id, u.version) for u in self.modules))


def make_offer(rel: Release, current: str,
               installed: List[InstalledModule]) -> Offer:
    if not rel.usable:
        return Offer(rel, current, False, [])
    return Offer(rel, current, is_newer(rel.version, current),
                 module_updates(rel, installed))


def should_show(offer: Offer, manual: bool, skipped: str = "") -> bool:
    """A manual check shows any update; the automatic one not one the user
    chose to skip."""
    if not offer.any:
        return False
    return manual or offer.key != (skipped or "")


def check_due(last: Optional[float], now: Optional[float] = None,
              interval: float = CHECK_INTERVAL) -> bool:
    """Whether the automatic check is due: never checked, a day passed, or
    the clock went backwards."""
    now = time.time() if now is None else now
    try:
        last = float(last)
    except (TypeError, ValueError):
        return True
    return last <= 0 or now - last >= interval or now < last


# ------------------------------------------------------------------ installed
def installed_modules() -> List[InstalledModule]:
    """The modules this app has loaded, with their versions."""
    from . import platforms
    from .modules import api, loader, registry
    recs = loader.installed_records()
    roots = loader.module_roots()
    out = []
    for mid, spec in registry.installed().items():
        rec = recs.get(mid) if mid in roots else None
        root = getattr(rec, "root", "") if rec is not None else ""
        info = platforms.MODULES.get(mid)
        out.append(InstalledModule(
            mid, info.name if info else spec.name, spec.version,
            "checksum" if spec.variant == api.CHECKSUM_ONLY else "full", root,
            bool(root) and os.path.isfile(os.path.join(root, "unins000.exe"))))
    return out


def install_kind() -> str:
    """``"inno"``: a Windows build installed by its setup.exe (it can update
    itself); ``"frozen"``: another built copy (the Linux packages, a zip);
    ``"source"``: run from source or pip (notify only)."""
    if not getattr(sys, "frozen", False):
        return "source"
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    if sys.platform == "win32" and _inno_installed(app_dir):
        return "inno"
    return "frozen"


INNO_APP_ID = "{94760517-1A60-48C1-8353-767DA53A52EB}"     # = AppId in installer.iss


def _inno_installed(app_dir: str) -> bool:
    if os.path.isfile(os.path.join(app_dir, "unins000.exe")):
        return True
    try:
        import winreg
    except ImportError:
        return False
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % INNO_APP_ID
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key) as k:
                loc = winreg.QueryValueEx(k, "InstallLocation")[0]
        except OSError:
            continue
        if os.path.normcase(os.path.abspath(loc)).rstrip("\\/") == \
                os.path.normcase(os.path.abspath(app_dir)).rstrip("\\/"):
            return True
    return False


# ------------------------------------------------------------------ checksums
def parse_sums(text) -> Dict[str, str]:
    """``sha256sum`` output (``<hex>  <name>`` or ``<hex> *<name>``) ->
    ``{name: hex}``. Blank lines and ``#`` comments are skipped; a path is
    reduced to its file name."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if m:
            out[m.group(2).strip().replace("\\", "/").rsplit("/", 1)[-1]] = m.group(1).lower()
    return out


def format_sums(sums: Dict[str, str]) -> str:
    return "".join("%s  %s\n" % (h, n) for n, h in sorted(sums.items()))


def verify_sums(data: bytes, sig: Optional[bytes], keys=None) -> Tuple[bool, str]:
    """Whether ``SHA256SUMS.txt`` (``data``) carries a valid Ed25519 signature
    by a key the app trusts for modules: ``(ok, key id or why not)``."""
    from .modules import signing
    return signing.verify(data, sig, keys)


@dataclass
class Checksums:
    """What the release says each asset's SHA-256 is."""
    sums: Dict[str, str] = field(default_factory=dict)
    signed: bool = False
    note: str = ""          # why the list is missing or unsigned ("" = signed)


def load_checksums(rel: Release, fetch: Callable[[str], bytes] = fetch_text,
                   keys=None) -> Checksums:
    """The release's checksum list. A list whose signature is present but
    wrong is an error (someone changed it); an unsigned list is used, with a
    note; no list means GitHub's own digests are used instead."""
    a = rel.asset(SUMS_NAME)
    if a is None:
        return Checksums(note="this release has no %s" % SUMS_NAME)
    data = fetch(a.url)
    sums = parse_sums(data)
    s = rel.asset(SIG_NAME)
    if s is None:
        return Checksums(sums, False, "%s is not signed" % SUMS_NAME)
    ok, who = verify_sums(data, fetch(s.url), keys)
    if not ok:
        if "cryptography" in who:
            return Checksums(sums, False, "the signature cannot be checked here (%s)" % who)
        raise UpdateError("the release's checksum list failed its signature "
                          "check (%s); nothing was installed" % who)
    return Checksums(sums, True, "")


def expected_sha256(asset: Asset, sums: Checksums) -> Tuple[str, str]:
    """``(hex, where from)``: the checksum list's, else GitHub's, else
    ``("", "")``."""
    h = sums.sums.get(asset.name)
    if h:
        return h, ("signed checksum list" if sums.signed else "checksum list")
    if asset.sha256:
        return asset.sha256, "GitHub's checksum"
    return "", ""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: str, asset: Asset, sums: Checksums) -> str:
    """Raise :class:`UpdateError` unless ``path`` is ``asset`` exactly; returns
    how it was verified ("" when nothing vouched for its contents)."""
    try:
        size = os.path.getsize(path)
    except OSError as ex:
        raise UpdateError("%s: %s" % (asset.name, ex))
    if asset.size and size != asset.size:
        raise UpdateError("%s is %d bytes, the release says %d"
                          % (asset.name, size, asset.size))
    want, src = expected_sha256(asset, sums)
    if not want:
        return ""
    if sha256_file(path) != want:
        raise UpdateError("%s does not match its SHA-256 in the %s; it was "
                          "not installed" % (asset.name, src))
    return src


# ------------------------------------------------------------------ download
def download_dir(version: str) -> str:
    """Where a release's files are downloaded (kept when the install is put
    off, so the next attempt does not fetch them again)."""
    return os.path.join(tempfile.gettempdir(), "%s-update-%s" % (branding.EXE_NAME, version))


def cleanup_downloads(current: str) -> None:
    """Remove download folders of versions this app already is (or is
    newer than) -- after an update has gone through."""
    import shutil
    base = tempfile.gettempdir()
    prefix = "%s-update-" % branding.EXE_NAME
    try:
        names = os.listdir(base)
    except OSError:
        return
    for n in names:
        if n.startswith(prefix):
            v = n[len(prefix):]
            try:
                if compare_versions(v, current) <= 0:
                    shutil.rmtree(os.path.join(base, n), ignore_errors=True)
            except ValueError:
                pass


def download(asset: Asset, folder: str, sums: Checksums,
             progress: Optional[Callable[[int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             opener=None, chunk: int = 1 << 16) -> Tuple[str, str]:
    """Download ``asset`` into ``folder`` and verify it: ``(path, how
    verified)``. A file already there that verifies is not fetched again.
    ``progress(bytes_so_far)``; ``cancelled()`` True stops it
    (:class:`UpdateError`)."""
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, asset.name)
    if os.path.isfile(dest):
        try:
            how = verify_file(dest, asset, sums)
            if progress:
                progress(asset.size)
            return dest, how
        except UpdateError:
            os.remove(dest)
    part = dest + ".part"
    opener = opener or (lambda u: _urlopen(u, 60.0, "application/octet-stream"))
    got = 0
    try:
        with opener(asset.url) as resp, open(part, "wb") as fh:
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("cancelled")
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                got += len(buf)
                if progress:
                    progress(got)
    except (urllib.error.URLError, OSError) as ex:
        _rm(part)
        raise UpdateError("could not download %s: %s" % (asset.name, getattr(ex, "reason", ex)))
    except UpdateError:
        _rm(part)
        raise
    try:
        how = verify_file(part, asset, sums)
    except UpdateError:
        _rm(part)
        raise
    os.replace(part, dest)
    return dest, how


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


# ------------------------------------------------------------------ Windows
def hold_app_mutex():
    """Windows: create the named mutex the installers' ``AppMutex`` looks
    for, held until the process exits (packaging/entry.py calls this for the
    app and every tool exe). Returns the handle, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        return k32.CreateMutexW(None, False, APP_MUTEX) or None
    except Exception:
        return None


def exe_names() -> List[str]:
    """Every executable of this bundle (they share its files)."""
    return [n + ".exe" for n in [branding.EXE_NAME, branding.MCP_EXE]
            + list(branding.TOOL_EXES.values())]


def parse_tasklist(text: str, exclude_pid: int) -> List[Tuple[str, int]]:
    """``tasklist /FO CSV /NH`` -> ``[(image, pid)]`` of this bundle's exes,
    other than ``exclude_pid``."""
    import csv
    import io
    want = {n.lower() for n in exe_names()}
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or row[0].lower() not in want:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        if pid != exclude_pid:
            out.append((row[0], pid))
    return out


def other_instances() -> List[Tuple[str, int]]:
    """Windows: this app's (and its tools') other running processes."""
    if sys.platform != "win32":
        return []
    import subprocess
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_tasklist(r.stdout, os.getpid())


def _bat_quote(path: str) -> str:
    return '"%s"' % path.replace("%", "%%")


#: Inno Setup's install mode switches: the update must go where the old
#: version is (a silent setup would otherwise install per user beside an
#: all-users copy)
CURRENT_USER, ALL_USERS = "/CURRENTUSER", "/ALLUSERS"


def install_mode(folder: str) -> str:
    """The Inno Setup switch that installs over what is in ``folder``: per
    user when it lies in the user's own folders (the default per-user
    install, %LOCALAPPDATA%), else for all users (Program Files; setup asks
    for administrator rights)."""
    from .modules import installer
    return CURRENT_USER if installer.app_dir_is_per_user(folder) else ALL_USERS


def chain_script(installers, app_exe: str, pid: int, log_dir: str = "") -> str:
    """The batch file that installs an update once this process has gone:
    wait for ``pid`` to exit (at most two minutes), run each Inno Setup
    installer silently in turn, then start the app again. The base
    installer, when it is the last one, relaunches the app itself
    (``/RELAUNCH=1``, see installer.iss); otherwise -- or when it failed --
    the script does. ``installers``: paths, or ``(path, mode)`` with mode
    :data:`CURRENT_USER`, :data:`ALL_USERS` or ""."""
    lines = ["@echo off", "chcp 65001 >nul",
             "rem %s update: generated by the app, safe to delete" % branding.APP_NAME,
             "set n=0", ":wait",
             'tasklist /FI "PID eq %d" /NH 2>nul | find "%d" >nul || goto go' % (pid, pid),
             "set /a n+=1", "if %n% geq 120 goto go",
             "ping -n 2 127.0.0.1 >nul", "goto wait", ":go"]
    for i, item in enumerate(installers):
        path, mode = (item, "") if isinstance(item, str) else item
        last = i == len(installers) - 1
        relaunch = last and _is_base_installer(path)
        args = list(INSTALLER_ARGS) + ([mode] if mode else [])
        if log_dir:
            args.append('/LOG=%s' % _bat_quote(os.path.join(
                log_dir, os.path.splitext(os.path.basename(path))[0] + ".log")))
        if relaunch:
            args.append(RELAUNCH_ARG)
        lines.append('start "" /wait %s %s' % (_bat_quote(path), " ".join(args)))
        if relaunch:
            lines.append('if errorlevel 1 start "" %s' % _bat_quote(app_exe))
            lines.append("goto end")
    lines.append('start "" %s' % _bat_quote(app_exe))
    lines.append(":end")
    return "\r\n".join(lines) + "\r\n"


def _is_base_installer(path: str) -> bool:
    n = path.replace("\\", "/").rsplit("/", 1)[-1]
    return bool(re.match(r"^%s-(Base-)?\d+(\.\d+)*-setup\.exe$"
                         % re.escape(branding.EXE_NAME), n))


def launch_chain(installers, folder: str) -> None:
    """Write the batch file and start it, detached and without a window."""
    import subprocess
    script = chain_script(installers, os.path.abspath(sys.executable), os.getpid(), folder)
    bat = os.path.join(folder, "install-update.cmd")
    with open(bat, "w", encoding="utf-8", newline="") as fh:
        fh.write(script)
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    cmd = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"),
           "/d", "/c", bat]
    try:
        subprocess.Popen(cmd, cwd=folder, close_fds=True, creationflags=flags
                         | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    except OSError:
        # a job that does not allow breaking away: start it inside the job
        subprocess.Popen(cmd, cwd=folder, close_fds=True, creationflags=flags)
