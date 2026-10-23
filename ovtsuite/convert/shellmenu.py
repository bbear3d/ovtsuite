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
* **Linux**: GNOME Files (a Nautilus script), Cinnamon Nemo (an action, by
  extension) and KDE Dolphin (a service menu).
* **macOS**: not offered -- Finder takes its menus from Quick Actions; drop
  files on OVT Converter in the Dock instead.

Generic extensions (.xml, .json, .csv) are left out so the menu does not
appear on every such file; those still open by dropping them on the window.
"""

from __future__ import annotations

import os
import shlex
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


# ------------------------------------------------------------------ Windows
def _win_cmdline(args: List[str]) -> str:
    return " ".join('"%s"' % a if (" " in a or not a) else a for a in args) + ' "%1"'


def _win_keys():
    return ["Software\\Classes\\SystemFileAssociations\\%s\\shell\\%s" % (e, VERB)
            for e in EXTENSIONS]


def _win_install() -> List[str]:
    import winreg
    cmd = command()
    line = _win_cmdline(cmd)
    for k in _win_keys():
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k, 0, winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "", 0, winreg.REG_SZ, LABEL)
            winreg.SetValueEx(h, "Icon", 0, winreg.REG_SZ, cmd[0] + ",0")
            # several files at once: one start per file, all handed to one window
            winreg.SetValueEx(h, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k + "\\command", 0,
                                winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "", 0, winreg.REG_SZ, line)
    _win_refresh()
    return ["Windows Explorer (%s)" % " ".join(EXTENSIONS)]


def _win_remove() -> List[str]:
    import winreg
    gone = 0
    for k in _win_keys():
        for sub in (k + "\\command", k):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                gone += sub == k
            except OSError:
                pass
    _win_refresh()
    return ["Windows Explorer"] if gone else []


def _win_installed() -> List[str]:
    import winreg
    for k in _win_keys():
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, k))
            return ["Windows Explorer"]
        except OSError:
            pass
    return []


def _win_refresh() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # ASSOCCHANGED
    except Exception:                                           # noqa: BLE001
        pass


# -------------------------------------------------------------------- Linux
def _data() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(_home(), ".local", "share")


def _linux_files():
    d = _data()
    return {
        "GNOME Files": os.path.join(d, "nautilus", "scripts", LABEL),
        "Nemo": os.path.join(d, "nemo", "actions", "%s-convert.nemo_action" % branding.PKG),
        "Dolphin": os.path.join(d, "kio", "servicemenus", "%s-convert.desktop" % branding.PKG),
    }


def _desktop_exec(args: List[str]) -> str:
    """An Exec= line: arguments quoted the Desktop Entry way."""
    out = []
    for a in args:
        if any(c in a for c in ' \t"\'\\$`'):
            a = '"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') \
                .replace("$", "\\$").replace("`", "\\`")
        out.append(a)
    return " ".join(out)


def _linux_texts():
    cmd = command()
    icon = "%s-converter" % branding.PKG
    exts = ";".join(e[1:] for e in EXTENSIONS) + ";"
    exts += ";".join(e[1:].upper() for e in EXTENSIONS) + ";"
    return {
        "GNOME Files": "#!/bin/sh\n# %s -- added by %s; remove it from the converter's menu\n"
                       "exec %s \"$@\"\n" % (LABEL, branding.APP_NAME,
                                             " ".join(shlex.quote(a) for a in cmd)),
        "Nemo": "[Nemo Action]\nName=%s\nComment=Open the selected files in %s\n"
                "Exec=%s %%F\nIcon-Name=%s\nSelection=notnone\nExtensions=%s\n"
                % (LABEL, branding.TOOL_NAMES.get("converter"), _desktop_exec(cmd), icon, exts),
        "Dolphin": "[Desktop Entry]\nType=Service\nMimeType=all/allfiles;\n"
                   "X-KDE-ServiceTypes=KonqPopupMenu/Plugin\nActions=convert;\n\n"
                   "[Desktop Action convert]\nName=%s\nIcon=%s\nExec=%s %%F\n"
                   % (LABEL, icon, _desktop_exec(cmd)),
    }


def _linux_install() -> List[str]:
    done = []
    texts = _linux_texts()
    for where, path in _linux_files().items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(texts[where])
        if where != "Nemo":          # a script / a KDE 6 service menu runs only if executable
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IRUSR)
        done.append(where)
    return done


def _linux_remove() -> List[str]:
    done = []
    for where, path in _linux_files().items():
        if os.path.isfile(path):
            os.remove(path)
            done.append(where)
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


def remove() -> List[str]:
    """Take the menu entry out again. Returns where it was."""
    try:
        if sys.platform.startswith("win"):
            return _win_remove()
        if sys.platform.startswith("linux"):
            return _linux_remove()
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
