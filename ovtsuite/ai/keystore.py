# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""API keys, kept in the operating system's own secret store.

* Windows: Credential Manager (``advapi32`` CredWrite/CredRead, via ctypes);
* macOS: the login Keychain (the ``security`` tool);
* Linux: the Secret Service -- GNOME Keyring, KWallet -- (``secret-tool``).

Where none is available (a minimal Linux without a keyring), the key goes in
a file only this user can read, and :func:`backend` says so, so the settings
dialog can tell the user. A key in the service's usual environment variable
(``ANTHROPIC_API_KEY`` ...) is used when nothing is stored.

Nothing here logs or prints a key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

from .. import branding

#: what keys are filed under in the OS store ("<app name> AI")
SERVICE = "%s AI" % branding.APP_NAME
FILE = "keys.json"


def _target(sid: str) -> str:
    return "%s/%s" % (SERVICE, sid)


# ------------------------------------------------------------------ Windows
def _win():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, adv


_GENERIC, _PERSIST_LOCAL_MACHINE = 1, 2


def _win_set(sid: str, key: str) -> None:
    ctypes, CREDENTIAL, adv = _win()
    blob = key.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL()
    c.Type = _GENERIC
    c.TargetName = _target(sid)
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = _PERSIST_LOCAL_MACHINE
    c.UserName = sid
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError("CredWrite failed (%d)" % ctypes.get_last_error())


def _win_get(sid: str) -> Optional[str]:
    ctypes, CREDENTIAL, adv = _win()
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(_target(sid), _GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        adv.CredFree(p)


def _win_delete(sid: str) -> None:
    ctypes, _C, adv = _win()
    adv.CredDeleteW(_target(sid), _GENERIC, 0)


# ------------------------------------------------------------------ macOS
def _mac_set(sid, key):
    subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE,
                    "-a", sid, "-w", key], check=True, capture_output=True)


def _mac_get(sid):
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE,
                        "-a", sid, "-w"], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _mac_delete(sid):
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", sid],
                   capture_output=True)


# ------------------------------------------------------------------ Secret Service
def _ss_ok() -> bool:
    if not shutil.which("secret-tool"):
        return False
    # a store exists only with a session bus; secret-tool hangs without one
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def _ss_set(sid, key):
    subprocess.run(["secret-tool", "store", "--label=%s %s" % (SERVICE, sid),
                    "service", SERVICE, "account", sid], input=key, text=True,
                   check=True, capture_output=True, timeout=20)


def _ss_get(sid):
    r = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "account", sid],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout else None


def _ss_delete(sid):
    subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", sid],
                   capture_output=True, timeout=20)


# ------------------------------------------------------------------ private file
def _file_path() -> str:
    from .config import ai_dir
    return os.path.join(ai_dir(), FILE)


def _file_load() -> dict:
    try:
        with open(_file_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_save(d: dict) -> None:
    path = _file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, path)


def _file_set(sid, key):
    d = _file_load()
    d[sid] = key
    _file_save(d)


def _file_get(sid):
    return _file_load().get(sid)


def _file_delete(sid):
    d = _file_load()
    if d.pop(sid, None) is not None:
        _file_save(d)


# ------------------------------------------------------------------ public
def backend() -> str:
    """"windows" | "macos" | "secret-service" | "file" (``OVT_AI_KEYSTORE``
    forces one -- the tests use "file")."""
    forced = os.environ.get("OVT_AI_KEYSTORE")
    if forced:
        return forced
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin" and shutil.which("security"):
        return "macos"
    if _ss_ok():
        return "secret-service"
    return "file"


DESCRIBE = {"windows": "Windows Credential Manager", "macos": "the macOS Keychain",
            "secret-service": "the system keyring",
            "file": "a private file in your settings folder (no system keyring found)"}

_OPS = {"windows": (_win_set, _win_get, _win_delete),
        "macos": (_mac_set, _mac_get, _mac_delete),
        "secret-service": (_ss_set, _ss_get, _ss_delete),
        "file": (_file_set, _file_get, _file_delete)}


def set_key(sid: str, key: str) -> str:
    """Store ``key``; returns where it went (for the dialog to say)."""
    key = (key or "").strip()
    if not key:
        delete_key(sid)
        return backend()
    b = backend()
    try:
        _OPS[b][0](sid, key)
    except Exception:
        if b == "file":
            raise
        b = "file"                     # the system store failed: keep it privately
        _file_set(sid, key)
    return b


def stored_key(sid: str) -> Optional[str]:
    b = backend()
    try:
        k = _OPS[b][1](sid)
    except Exception:
        k = None
    if not k and b != "file":
        k = _file_get(sid)             # a key that fell back to the file
    return k or None


def get_key(sid: str) -> Optional[str]:
    """The key to use: the stored one, else the service's environment variable."""
    k = stored_key(sid)
    if k:
        return k
    from .providers import SPECS
    spec = SPECS.get(sid)
    if spec is not None and spec.env:
        return os.environ.get(spec.env) or None
    return None


def key_source(sid: str) -> str:
    """"stored" | "environment" | "" -- for the dialog."""
    if stored_key(sid):
        return "stored"
    from .providers import SPECS
    spec = SPECS.get(sid)
    if spec is not None and spec.env and os.environ.get(spec.env):
        return "environment"
    return ""


def delete_key(sid: str) -> None:
    b = backend()
    try:
        _OPS[b][2](sid)
    except Exception:
        pass
    if b != "file":
        _file_delete(sid)
