# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TunerPro checksum plugins for XDF export.

The base app never computes a checksum. It lists the plugins installed
modules declare (:class:`ovtsuite.modules.api.TunerProPlugin`), writes the
XDFCHECKSUM block that names one, carries a source XDF's blocks through, or
names a TunerPro plugin installed on this PC (``Documents\\TunerPro
Files\\Plugins``). Each plugin DLL is read statically, as a PE file, never
loaded or run, for its id, its name and its calculation methods. An XDF
names a plugin by its id and a method by its index -- never by the DLL's
path -- so that is what the block carries.
"""

from __future__ import annotations

import os
import re
import struct
from typing import List, Optional, Tuple

#: job.checksum values that are not a descriptor id
NONE, KEEP, AUTO = "none", "keep", "auto"
DLL = "dll:"
#: ``tp:<plugin id>:<method index>`` -- a TunerPro plugin and one of its
#: calculation methods (see :func:`installed_plugins`)
TP = "tp:"


def descriptors() -> list:
    """Every TunerPro plugin the installed modules declare."""
    out = []
    try:
        from ..modules import registry
        mods = registry.installed().values()
    except Exception:
        return out
    for m in mods:
        if not getattr(m, "tunerpro", None):
            continue
        try:
            out.extend(m.call("tunerpro") or [])
        except Exception:                  # noqa: BLE001 - one module's fault
            continue
    return out


def descriptor(pid: str):
    return next((d for d in descriptors() if d.id == pid), None)


def block(d, size: int) -> dict:
    """The XDFCHECKSUM fields for descriptor ``d`` on an image of ``size``."""
    out = {"title": d.name, "pluginmoduleid": d.plugin_id}
    for k, v in d.fields:
        out[k] = v.format(end=max(0, size - 1), size=size)
    return out


def platform_of(image: Optional[bytes]) -> Optional[str]:
    if image is None:
        return None
    try:
        from ..platforms.identify import identify
        ident = identify(image)
        return ident.platform if ident.known else None
    except Exception:
        return None


def for_export(job, defn, image: Optional[bytes], warnings: List[str]) -> List[dict]:
    """The XDFCHECKSUM blocks the XDF gets for ``job.checksum``."""
    choice = (job.checksum or NONE).strip()
    size = int(defn.info.get("image_size") or 0) or (len(image) if image else 0)
    if choice == NONE:
        return []
    if choice == KEEP:
        if not defn.checksums:
            warnings.append("Keep checksum blocks: the source has none.")
        return [dict(c) for c in defn.checksums]
    if choice.startswith(TP):
        b = tp_block(choice, size, warnings)
        return [b] if b else []
    if choice.startswith(DLL):
        path = choice[len(DLL):]
        info = read_plugin(path)
        if not info.guids:
            warnings.append("No plugin id found in %s -- no checksum block written."
                            % os.path.basename(path))
            return []
        if len(info.guids) > 1:
            warnings.append("%s names %d plugin ids; the first (%s) was written."
                            % (os.path.basename(path), len(info.guids), info.guids[0]))
        return [{"title": info.name or os.path.splitext(os.path.basename(path))[0],
                 "pluginmoduleid": info.guids[0], "datastart": "0x0",
                 "dataend": "0x%X" % max(0, size - 1), "storeaddress": "0x0",
                 "calculationmethod": "0x0"}]
    if choice == AUTO:
        pid = platform_of(image)
        if pid is None:
            if defn.checksums:
                warnings.append("Checksum: no binary to identify -- kept the "
                                "source's checksum blocks.")
                return [dict(c) for c in defn.checksums]
            warnings.append("Checksum: Auto needs a binary to identify the "
                            "platform -- no block written.")
            return []
        cands = [d for d in descriptors() if pid in d.families]
        if not cands:
            tp = installed_for(pid)
            if tp is not None:
                b = tp_block(tp, size, warnings)
                return [b] if b else []
            warnings.append("Checksum: no installed module or TunerPro plugin "
                            "offers a checksum for %s -- no block written." % pid)
            return []
        d = next((c for c in cands if not c.sizes or size in c.sizes), None)
        if d is None:
            warnings.append("Checksum: %s does not handle a 0x%X-byte image -- no "
                            "block written. %s" % (cands[0].name, size, cands[0].limitations))
            return []
    else:
        d = descriptor(choice)
        if d is None:
            warnings.append("Checksum plugin %r is not offered by any installed "
                            "module -- no block written." % choice)
            return []
        if d.sizes and size not in d.sizes:
            warnings.append("Checksum: %s does not handle a 0x%X-byte image -- no "
                            "block written. %s" % (d.name, size, d.limitations))
            return []
    if d.limitations:
        warnings.append("Checksum plugin %s: %s" % (d.name, d.limitations))
        defn.info["checksum_note"] = "Checksum plugin: " + d.limitations
    return [block(d, size)]


# ------------------------------------------------------------ PE, statically
class PluginInfo:
    def __init__(self):
        self.guids: List[str] = []
        self.name = ""
        self.machine = ""


_GUID = re.compile(rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                   rb"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def _sections(data: bytes) -> List[Tuple[str, int, int, int]]:
    """``[(name, raw offset, raw size, characteristics)]`` of a PE file."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise ValueError("not a Windows DLL (no MZ header)")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a Windows DLL (no PE header)")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    sec = pe + 24 + opt
    out = []
    for i in range(nsec):
        o = sec + 40 * i
        name = data[o:o + 8].rstrip(b"\0").decode("latin-1")
        rsize, roff = struct.unpack_from("<II", data, o + 16)
        chars = struct.unpack_from("<I", data, o + 36)[0]
        out.append((name, roff, rsize, chars))
    return out


def read_plugin(path: str) -> PluginInfo:
    """The plugin ids (GUID strings) a plugin DLL holds in its data sections,
    and its file description -- read from the file, never loaded."""
    info = PluginInfo()
    with open(path, "rb") as fh:
        data = fh.read()
    secs = _sections(data)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    info.machine = {0x14C: "x86", 0x8664: "x64"}.get(
        struct.unpack_from("<H", data, pe + 4)[0], "")
    seen = []
    for name, off, size, chars in secs:
        if chars & 0x20000000:            # executable code: not where data is
            continue
        blob = data[off:off + size]
        for m in _GUID.finditer(blob):
            g = m.group(0).decode().lower()
            if g not in seen:
                seen.append(g)
        wide = blob.replace(b"\0", b"") if b"-\0" in blob else b""
        for m in _GUID.finditer(wide):
            g = m.group(0).decode().lower()
            if g not in seen:
                seen.append(g)
    info.guids = seen
    i = data.find("FileDescription".encode("utf-16-le"))
    if i >= 0:
        j = i + len("FileDescription".encode("utf-16-le"))
        while j < len(data) - 1 and data[j:j + 2] == b"\0\0":
            j += 2
        k = data.find(b"\0\0\0", j)
        if k > j:
            info.name = data[j:k + 1].decode("utf-16-le", "replace").strip("\0 ")
    return info


# --------------------------------------------- TunerPro plugins on this PC
_GUID_TXT = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                       r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


class InstalledPlugin:
    """A TunerPro checksum plugin found on this PC."""

    def __init__(self, path: str, uuid: str, name: str, desc: str,
                 methods: List[str]):
        self.path, self.uuid, self.name, self.desc = path, uuid, name, desc
        self.methods = methods

    def choice(self, method: int = 0) -> str:
        """The ``job.checksum`` value for one of its methods."""
        return "%s%s:%d" % (TP, self.uuid, method)

    def __repr__(self):
        return "InstalledPlugin(%r, %s, %r)" % (self.name, self.uuid, self.methods)


def tp_block(choice: str, size: int, warnings: List[str]) -> Optional[dict]:
    """The XDFCHECKSUM fields for a ``tp:<id>:<method>`` choice: the
    plugin's id, the method's index (TunerPro's ``calculationmethod``) and
    the whole image as the region. The method's name is the block's title
    when the plugin is installed here."""
    pid, _sep, meth = choice[len(TP):].partition(":")
    try:
        mi = int(meth or "0", 0)
    except ValueError:
        mi = 0
    pid = pid.strip("{} ").upper()
    if not _GUID_TXT.match(pid):
        warnings.append("Checksum: %r is not a plugin id -- no block written." % pid)
        return None
    plug = next((q for q in installed_plugins() if q.uuid == pid), None)
    title = "Checksum"
    if plug is not None and 0 <= mi < len(plug.methods):
        title = plug.methods[mi]
    elif plug is None:
        warnings.append("Checksum: plugin %s is not installed on this PC; the "
                        "block names it all the same." % pid)
    return {"title": title, "pluginmoduleid": pid, "datastart": "0x0",
            "dataend": "0x%X" % max(0, size - 1), "storeaddress": "0x0",
            "calculationmethod": "0x%X" % mi}


def installed_for(platform: str) -> Optional[str]:
    """The ``tp:`` choice of an installed TunerPro plugin method made for
    ``platform`` ('edc16' -> a method named for EDC16), or None. A method
    for the ECU's EEPROM is never picked for its flash image."""
    key = (platform or "").upper()
    if not key:
        return None
    for p in installed_plugins():
        for i, m in enumerate(p.methods):
            mu = m.upper()
            if key in mu.replace(" ", "") and "EEPROM" not in mu:
                return p.choice(i)
    return None


def _documents() -> str:
    """The user's Documents folder (where TunerPro keeps its files), even
    when it is redirected (OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PERSONAL = 5
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 \
                    and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def plugin_dirs(extra=()) -> List[str]:
    """The folders TunerPro loads plugins from, that exist."""
    cands = list(extra) + [os.path.join(_documents(), "TunerPro Files", "Plugins"),
                           os.path.join(os.path.expanduser("~"), "Documents",
                                        "TunerPro Files", "Plugins")]
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "TunerPro RT", "Plugins"))
    out, seen = [], set()
    for d in cands:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


_SCAN = {}


def installed_plugins(extra=()) -> List[InstalledPlugin]:
    """Every checksum plugin in TunerPro's plugin folders (other plugins,
    such as the ELM327 one, are left out). A DLL is read again only when
    it changes."""
    out = []
    for d in plugin_dirs(extra):
        try:
            names = sorted(n for n in os.listdir(d) if n.lower().endswith(".dll"))
        except OSError:
            continue
        for n in names:
            path = os.path.join(d, n)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (os.path.normcase(path), st.st_size, st.st_mtime)
            if key not in _SCAN:
                try:
                    _SCAN[key] = parse_plugin(path)
                except Exception:              # noqa: BLE001 - an unreadable DLL
                    _SCAN[key] = None
            p = _SCAN[key]
            if p is not None and all(q.uuid != p.uuid for q in out):
                out.append(p)
    return out


def _exports(data: bytes, rva2off, eoff: int) -> dict:
    """{name: function RVA} of the PE export directory at file offset ``eoff``."""
    nnames, funcs, names, ords = struct.unpack_from("<IIII", data, eoff + 24)
    foff, noff, ooff = rva2off(funcs), rva2off(names), rva2off(ords)
    out = {}
    if noff is None:
        return out
    for i in range(nnames):
        if noff + 4 * i + 4 > len(data):
            break
        o = rva2off(struct.unpack_from("<I", data, noff + 4 * i)[0])
        if o is None:
            continue
        end = data.find(b"\0", o)
        if end < 0:
            continue
        name = data[o:end].decode("ascii", "replace")
        rva = None
        if foff is not None and ooff is not None and ooff + 2 * i + 2 <= len(data):
            idx = struct.unpack_from("<H", data, ooff + 2 * i)[0]
            if foff + 4 * idx + 4 <= len(data):
                rva = struct.unpack_from("<I", data, foff + 4 * idx)[0]
        out[name] = rva
    return out


def _method_count(fb: bytes) -> Optional[int]:
    """How many methods ``MMCSGetChecksumPluginInfo`` reports: the count it
    stores in the info structure's field at +0x18."""
    regs = (0x40, 0x41, 0x42, 0x43, 0x45, 0x46, 0x47)
    for off in range(len(fb) - 6):          # mov dword [reg+18h], imm32
        if fb[off] == 0xC7 and fb[off + 2] == 0x18 and fb[off + 1] in regs:
            n = struct.unpack_from("<I", fb, off + 3)[0]
            if 1 <= n <= 32:
                return n
    for off in range(len(fb) - 34):         # mov reg, imm32 .. mov [reg+18h], reg
        if not 0xB8 <= fb[off] <= 0xBF:
            continue
        n = struct.unpack_from("<I", fb, off + 1)[0]
        if not 1 <= n <= 32:
            continue
        for j in range(5, 35):
            if off + j + 2 >= len(fb):
                break
            if fb[off + j] == 0x89 and fb[off + j + 2] == 0x18 \
                    and fb[off + j + 1] in regs:
                return n
    return None


_VER = re.compile(r"^\S{1,20}$")
_VER_DIGIT = re.compile(r"\d[.\-]\d|\d$")
_ERR = re.compile(r"invalid|expected|error", re.I)


def _is_ver(t: str) -> bool:
    return bool(_VER.match(t) and _VER_DIGIT.search(t)
                and any(c.isdigit() for c in t))


def _noise(t: str) -> bool:
    tl = t.lower()
    return (t.startswith(".") or tl.endswith(".dll") or t.startswith("MMCS")
            or t.startswith("__") or ":\\" in t or t == "GCTL")


def parse_plugin(path: str) -> Optional[InstalledPlugin]:
    """A TunerPro checksum plugin's id, name and methods, read statically
    from its DLL (None for a DLL that is not one). TunerPro plugins are
    32-bit and export ``MMCSGetChecksumPluginInfo`` and
    ``MMCSGetChecksumInfo``; the strings they report follow the id in the
    data section: name, description, then each method's name and
    description."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 + 100 >= len(data) or data[pe:pe + 4] != b"PE\0\0":
        return None
    machine, nsec = struct.unpack_from("<HH", data, pe + 4)
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    if machine != 0x14C:
        return None
    export_rva = struct.unpack_from("<I", data, pe + 24 + 96)[0]
    if not export_rva:
        return None
    secs = []
    so = pe + 24 + opt
    for i in range(nsec):
        o = so + 40 * i
        if o + 40 > len(data):
            break
        _vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
        secs.append((va, raw, rsize))

    def rva2off(rva):
        for va, raw, sz in secs:
            if va <= rva < va + sz:
                return raw + (rva - va)
        return None

    eoff = rva2off(export_rva)
    if eoff is None or eoff + 40 > len(data):
        return None
    exp = _exports(data, rva2off, eoff)
    if "MMCSGetChecksumPluginInfo" not in exp or "MMCSGetChecksumInfo" not in exp:
        return None
    count = None
    rva = exp.get("MMCSGetChecksumPluginInfo")
    fo = rva2off(rva) if rva is not None else None
    if fo is not None:
        count = _method_count(data[fo:fo + 300])

    strings = [m.group(0).decode("latin-1")
               for m in re.finditer(rb"[\x20-\x7e]{4,}(?=\x00|$)", data)]
    ui = next((i for i, t in enumerate(strings) if _GUID_TXT.match(t)), -1)
    if ui < 0:
        return None
    uuid = strings[ui].upper()
    post = []
    for t in strings[ui + 1:]:
        if _noise(t):
            break
        post.append(t)
    ni = next((i for i, t in enumerate(post) if " " in t and "@" not in t
               and not _is_ver(t) and len(t) <= 60), -1)
    if ni < 0:
        return None
    name = post[ni]
    di = next((i for i in range(ni + 1, len(post))
               if not _is_ver(post[i]) and "@" not in post[i]), -1)
    desc = post[di] if di >= 0 else ""
    methods = []
    i = max(di, ni) + 1
    while i < len(post):
        if count is not None and len(methods) >= count:
            break
        t = post[i]
        if _ERR.search(t):
            break
        if _is_ver(t) or "@" in t or " " not in t or len(t) > 80:
            i += 1
            continue
        methods.append(t)
        i += 1
        while i < len(post):                 # skip the method's description
            if _ERR.search(post[i]):
                break
            if _is_ver(post[i]):
                i += 1
                continue
            i += 1
            break
    if not methods:
        return None
    return InstalledPlugin(path, uuid, name, desc, methods)
