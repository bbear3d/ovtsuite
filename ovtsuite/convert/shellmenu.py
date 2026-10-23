# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""\"Convert with OVT Converter\" in the file manager's right-click menu.

Opt-in (the converter window's menu, ``ovtsuite convert --shell-menu``, or
the installer's unticked task) and for the current user only. It adds a
menu entry and nothing else: no file association, so double-clicking a file
still opens whatever opened it before.

* **Windows**: a verb under ``HKCU\\Software\\Classes\\SystemFileAssociations
  \\<.ext>\\shell`` for the converter's own extensions -- the key Windows
  keeps for extra verbs beside a type's own program. Windows 11 lists it
  under *Show more options*. Several files right-clicked together all open
  in one converter window (the converter hands later starts to the first).
  Each install (the app, the standalone converter) has its own entry,
  ``<VERB>.<install>``; while one shows, another waits hidden, and
  uninstalling one shows the other again.
* **Linux**: KDE Dolphin (a service menu) and GNOME Files ("Open With", an
  application never made the default) for the converter's own MIME types,
  registered by extension; Cinnamon Nemo (an action, by extension).
* **macOS**: not offered -- Finder takes its menus from Quick Actions; drop
  files on OVT Converter in the Dock instead.

Generic extensions (.xml, .json, .csv) are left out so the menu does not
appear on every such file; those still open by dropping them on the window.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from typing import List

from .. import branding

LABEL = "Convert with %s" % branding.TOOL_NAMES.get("converter", "OVT Converter")
VERB = "%s.Convert" % branding.EXE_NAME
#: definitions, value files and images the converter reads
EXTENSIONS = (".a2l", ".dam", ".ols", ".kp", ".xdf", ".dcm", ".cdfx", ".cdf",
              ".hex", ".ihex", ".s19", ".s28", ".s37", ".mot", ".srec", ".bin", ".ori",
              ".mod")


class ShellMenuError(RuntimeError):
    pass


def supported() -> bool:
    return sys.platform.startswith("win") or sys.platform.startswith("linux")


def command() -> List[str]:
    """How the file manager starts the converter (files follow)."""
    exe = sys.executable
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(exe))
        tool = branding.TOOL_EXES.get("converter", "OVTConverter")
        for name in (tool + ".exe", tool):
            p = os.path.join(here, name)
            if os.path.isfile(p) and os.path.normcase(p) != os.path.normcase(exe):
                return [p]
        return [exe, "--converter"]
    if sys.platform.startswith("win"):
        # pythonw: no console window behind the converter
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(w):
            exe = w
    return [exe, "-m", "ovtsuite", "--converter"]


def _home() -> str:
    return os.path.expanduser("~")


def _install_dir() -> str:
    """The folder of the program the menu starts: one install's identity."""
    return os.path.normcase(os.path.dirname(os.path.abspath(command()[0])))


def _tag(folder: str = "") -> str:
    """A short, stable name for one install (the main app and the
    standalone converter each have their own menu entry, so uninstalling
    one never takes the other's away)."""
    return hashlib.sha1((folder or _install_dir()).encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------ Windows
#: Explorer leaves a verb carrying this value out of the menu: an install's
#: entry waits, hidden, while another install's is shown
_HIDE = "LegacyDisable"
_CLASSES = "Software\\Classes\\SystemFileAssociations\\%s\\shell"


def _win_cmdline(args: List[str]) -> str:
    return " ".join('"%s"' % a if (" " in a or not a) else a for a in args) + ' "%1"'


def _win_keys(verb: str = ""):
    """This install's verb key for every extension."""
    verb = verb or "%s.%s" % (VERB, _tag())
    return [(_CLASSES % e) + "\\" + verb for e in EXTENSIONS]


def _cmd_exe(line: str) -> str:
    """The program a stored command line starts."""
    line = line.strip()
    if line.startswith('"'):
        return line[1:].split('"', 1)[0]
    return line.split(" ", 1)[0]


def _win_entries(root=None, prefix: str = ""):
    """``{verb: (exe, hidden)}`` of every install's entry under ``root``
    (HKCU, or a user's hive under HKEY_USERS with ``prefix``), read from the
    first extension's key -- every install writes them all."""
    import winreg
    root = winreg.HKEY_CURRENT_USER if root is None else root
    out = {}
    base = prefix + (_CLASSES % EXTENSIONS[0])
    try:
        h = winreg.OpenKey(root, base)
    except OSError:
        return out
    with h:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(h, i)
            except OSError:
                break
            i += 1
            if name != VERB and not name.startswith(VERB + "."):
                continue
            try:
                with winreg.OpenKey(h, name + "\\command") as c:
                    exe = _cmd_exe(winreg.QueryValueEx(c, "")[0])
            except OSError:
                exe = ""
            hidden = False
            try:
                with winreg.OpenKey(h, name) as v:
                    winreg.QueryValueEx(v, _HIDE)
                    hidden = True
            except OSError:
                pass
            out[name] = (exe, hidden)
    return out


def _win_delete_verb(verb: str, root=None, prefix: str = "") -> None:
    import winreg
    root = winreg.HKEY_CURRENT_USER if root is None else root
    for e in EXTENSIONS:
        k = prefix + (_CLASSES % e) + "\\" + verb
        for sub in (k + "\\command", k):
            try:
                winreg.DeleteKey(root, sub)
            except OSError:
                pass


def _win_set_hidden(verb: str, hidden: bool, root=None, prefix: str = "") -> None:
    import winreg
    root = winreg.HKEY_CURRENT_USER if root is None else root
    for e in EXTENSIONS:
        k = prefix + (_CLASSES % e) + "\\" + verb
        try:
            with winreg.OpenKey(root, k, 0, winreg.KEY_WRITE) as h:
                if hidden:
                    winreg.SetValueEx(h, _HIDE, 0, winreg.REG_SZ, "")
                else:
                    try:
                        winreg.DeleteValue(h, _HIDE)
                    except OSError:
                        pass
        except OSError:
            pass


def _alive(exe: str) -> bool:
    return bool(exe) and os.path.isfile(exe)


def _win_install() -> List[str]:
    import winreg
    cmd = command()
    line = _win_cmdline(cmd)
    mine = "%s.%s" % (VERB, _tag())
    others = {}
    for verb, (exe, hidden) in _win_entries().items():
        if verb == mine:
            continue
        if verb == VERB or not _alive(exe):
            _win_delete_verb(verb)          # the old shared key, or a removed install's
        else:
            others[verb] = hidden
    # another install's entry already shows: this one waits behind it, so
    # the menu never lists the converter twice
    hide = any(not h for h in others.values())
    for k in _win_keys(mine):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k, 0, winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "", 0, winreg.REG_SZ, LABEL)
            winreg.SetValueEx(h, "Icon", 0, winreg.REG_SZ, cmd[0] + ",0")
            # several files at once: one start per file, all handed to one window
            winreg.SetValueEx(h, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k + "\\command", 0,
                                winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "", 0, winreg.REG_SZ, line)
    _win_set_hidden(mine, hide)
    _win_refresh()
    return ["Windows Explorer (%s)" % " ".join(EXTENSIONS)]


def _user_hives():
    """``(HKEY_USERS, "<sid>\\\\")`` for every loaded user profile: an
    elevated uninstall runs as the administrator, but the menu was added for
    the user who ran setup."""
    import winreg
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, "") as h:
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(h, i)
                except OSError:
                    break
                i += 1
                if sid.startswith("S-1-5-21-") and not sid.endswith("_Classes"):
                    out.append((winreg.HKEY_USERS, sid + "\\"))
    except OSError:
        pass
    return out


def _win_remove(everywhere: bool = False) -> List[str]:
    """Remove this install's entry -- in every loaded user's profile too,
    where this process may write them -- and show another install's if one
    was waiting. ``everywhere``: every install's (the user turning the menu
    off)."""
    gone = 0
    mine = "%s.%s" % (VERB, _tag())
    for root, prefix in [(None, "")] + _user_hives():
        try:
            entries = _win_entries(root, prefix)
        except OSError:
            continue
        for verb, (exe, _hidden) in entries.items():
            ours = verb == mine or verb == VERB and (
                not exe or os.path.normcase(os.path.dirname(exe)) == _install_dir())
            if everywhere and root is None or ours or not _alive(exe):
                _win_delete_verb(verb, root, prefix)
                gone += 1
        rest = [v for v, (exe, h) in _win_entries(root, prefix).items() if _alive(exe)]
        if rest and all(_win_entries(root, prefix)[v][1] for v in rest):
            _win_set_hidden(rest[0], False, root, prefix)   # a waiting one takes over
    _win_refresh()
    return ["Windows Explorer"] if gone else []


def _win_installed() -> List[str]:
    return ["Windows Explorer"] if any(_alive(exe) for exe, _h in _win_entries().values()) \
        else []


def _win_refresh() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # ASSOCCHANGED
    except Exception:                                           # noqa: BLE001
        pass


# -------------------------------------------------------------------- Linux
#: own MIME types for the converter's files, so the KDE and GNOME entries
#: show for those files only. Their globs are weighted below every standard
#: type's: a file another type already claims (.dcm: DICOM, .mod: tracker
#: music) keeps it, and that type is listed too.
_MIME_PREFIX = "application/x-ovt-"
_CLAIMED = {".dcm": "application/dicom", ".cdf": "application/x-netcdf",
            ".mod": "audio/x-mod", ".bin": "application/octet-stream",
            ".hex": "text/x-hex", ".ihex": "text/x-hex"}


def _data() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(_home(), ".local", "share")


def _mime_types() -> List[str]:
    own = ["%s%s" % (_MIME_PREFIX, e[1:]) for e in EXTENSIONS]
    return own + sorted(set(_CLAIMED.values()))


def _linux_files():
    d = _data()
    return {
        "GNOME Files": os.path.join(d, "applications", "%s-convert.desktop" % branding.PKG),
        "Nemo": os.path.join(d, "nemo", "actions", "%s-convert.nemo_action" % branding.PKG),
        "Dolphin": os.path.join(d, "kio", "servicemenus", "%s-convert.desktop" % branding.PKG),
    }


def _mime_package() -> str:
    return os.path.join(_data(), "mime", "packages", "%s-converter.xml" % branding.PKG)


def _legacy_script() -> str:
    """The Nautilus script an earlier version added (shown on every file)."""
    return os.path.join(_data(), "nautilus", "scripts", LABEL)


def _desktop_exec(args: List[str]) -> str:
    """An Exec= line: arguments quoted the Desktop Entry way."""
    out = []
    for a in args:
        if any(c in a for c in ' \t"\'\\$`'):
            a = '"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') \
                .replace("$", "\\$").replace("`", "\\`")
        out.append(a)
    return " ".join(out)


def _mime_xml() -> str:
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<!-- added by %s for "%s"; removed with it -->' % (branding.APP_NAME, LABEL),
            '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">']
    for e in EXTENSIONS:
        rows.append('  <mime-type type="%s%s">' % (_MIME_PREFIX, e[1:]))
        rows.append('    <comment>%s file (%s)</comment>' % (e[1:].upper(), branding.APP_NAME))
        rows.append('    <glob pattern="*%s" weight="10"/>' % e)
        rows.append('  </mime-type>')
    rows.append('</mime-info>')
    return "\n".join(rows) + "\n"


def _linux_texts():
    cmd = command()
    icon = "%s-converter" % branding.PKG
    exts = ";".join(e[1:] for e in EXTENSIONS) + ";"
    exts += ";".join(e[1:].upper() for e in EXTENSIONS) + ";"
    mimes = ";".join(_mime_types()) + ";"
    return {
        # GNOME Files has no filtered menu entries of its own: an application
        # for these types only, under "Open With" -- never their default
        "GNOME Files": "[Desktop Entry]\nType=Application\nName=%s\n"
                       "Comment=Open the selected files in %s\nExec=%s %%F\nIcon=%s\n"
                       "MimeType=%s\nNoDisplay=true\nTerminal=false\n"
                       % (LABEL, branding.TOOL_NAMES.get("converter"), _desktop_exec(cmd),
                          icon, mimes),
        "Nemo": "[Nemo Action]\nName=%s\nComment=Open the selected files in %s\n"
                "Exec=%s %%F\nIcon-Name=%s\nSelection=notnone\nExtensions=%s\n"
                % (LABEL, branding.TOOL_NAMES.get("converter"), _desktop_exec(cmd), icon, exts),
        "Dolphin": "[Desktop Entry]\nType=Service\nMimeType=%s\n"
                   "X-KDE-ServiceTypes=KonqPopupMenu/Plugin\nActions=convert;\n\n"
                   "[Desktop Action convert]\nName=%s\nIcon=%s\nExec=%s %%F\n"
                   % (mimes, LABEL, icon, _desktop_exec(cmd)),
    }


def _refresh_mime() -> None:
    import shutil
    import subprocess
    for tool, arg in (("update-mime-database", os.path.join(_data(), "mime")),
                      ("update-desktop-database", os.path.join(_data(), "applications"))):
        exe = shutil.which(tool)
        if exe and os.path.isdir(arg):
            try:
                subprocess.run([exe, arg], timeout=60, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass


def _linux_install() -> List[str]:
    done = []
    texts = _linux_texts()
    pkg = _mime_package()
    os.makedirs(os.path.dirname(pkg), exist_ok=True)
    with open(pkg, "w", encoding="utf-8") as fh:
        fh.write(_mime_xml())
    if os.path.isfile(_legacy_script()):
        os.remove(_legacy_script())
    for where, path in _linux_files().items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(texts[where])
        if where == "Dolphin":          # a KDE 6 service menu runs only if executable
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IRUSR)
        done.append(where)
    _refresh_mime()
    return done


def _linux_remove(everywhere: bool = False) -> List[str]:
    done = []
    for where, path in _linux_files().items():
        if os.path.isfile(path):
            os.remove(path)
            done.append(where)
    for extra in (_mime_package(), _legacy_script()):
        if os.path.isfile(extra):
            os.remove(extra)
    _refresh_mime()
    return done


def _linux_installed() -> List[str]:
    return [w for w, p in _linux_files().items() if os.path.isfile(p)]


# --------------------------------------------------------------------- API
def install() -> List[str]:
    """Add the menu entry. Returns where it went."""
    try:
        if sys.platform.startswith("win"):
            return _win_install()
        if sys.platform.startswith("linux"):
            return _linux_install()
    except OSError as exc:
        raise ShellMenuError(str(exc))
    raise ShellMenuError("No right-click menu on this system: drop files on %s instead."
                         % branding.TOOL_NAMES.get("converter", "OVT Converter"))


def remove(everywhere: bool = False) -> List[str]:
    """Take the menu entry out again. Returns where it was. An uninstaller
    removes its own install's entry (another install's stays, and shows if
    it was waiting); ``everywhere`` -- the user turning the menu off --
    removes every install's."""
    try:
        if sys.platform.startswith("win"):
            return _win_remove(everywhere)
        if sys.platform.startswith("linux"):
            return _linux_remove(everywhere)
    except OSError as exc:
        raise ShellMenuError(str(exc))
    return []


def installed() -> List[str]:
    try:
        if sys.platform.startswith("win"):
            return _win_installed()
        if sys.platform.startswith("linux"):
            return _linux_installed()
    except OSError:
        pass
    return []
