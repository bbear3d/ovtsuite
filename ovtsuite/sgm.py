# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r'''VW "SW-CNT" data container (``.sgm``) -- read-only.

The XML source an ``.sgo`` is built from: the ``<!DOCTYPE SW-CNT ... Datencontainer
fuer die SG-Programmierung>`` document VW's flash tooling ships. Read here from
the three EDC17 containers in the DAMOS packages (03L906022LS 3610, 03L906022QF
6211, 03L906022KB 7088); every block of all three decodes and verifies.

Layout, as far as the files use it::

    SW-CNT
      IDENT           CNT-DATEI, CNT-VERSION-TYP/-INHALT, SW-VERSION-KURZ/-LANG,
                      CNT-IDENT-TEXT: tab-separated "Kundennummer  03L906022LS",
                      "Bosch SW-TTNR", "Bosch HW-TTNR", "Checksum lin"
      INFO            contact addresses and a revision list (not read: personal data)
      ABLAEUFE/ABLAUF the flashing sequence: KWP-2000 baud rates, timing, the
                      SA2 security "tape" (12 comma-separated bytes), and
                      DATEN-VERWEISE, the block names in flashing order
      DATEN/DATENBLOECKE/DATENBLOCK, one per area:
          DATENBLOCK-NAME, DATENBLOCK-FORMAT-NAME (dfn_mime)
          START-ADR                   flash address of the area
          DATENBLOCK-FORMAT           0x11
          GROESSE-DEKOMPRIMIERT       decoded length
          LOESCH-BEREICH              the range erased before writing
          DATENBLOCK-CHECK            START-ADR, END-ADR, CHECKSUMME (16 bit)
          DATENBLOCK-DATEN            a MIME header (``name="..bc1"``,
                                      ``Content-Transfer-Encoding: base64``)
                                      and the data in base64

The base64 is the same stream an ``.sgo`` stores, minus the ``.sgo``'s outer
XOR 0xFF: a Bosch BCB Type1 area, with the provenance header in the clear
followed by ``1A 01`` and the records. So an ``.sgo`` section header is these
fields in order -- address, format, length, erase start and end, check start and
end, the 16-bit checksum, stored length -- and :func:`parse` builds exactly that
section from each block and hands the sections to :func:`ovtsuite.sgo.assemble`.
Decoding, verification (terminal 24-bit sum, the sum the header restates, the
16-bit ``CHECKSUMME``) and placement in :func:`ovtsuite.sgo.image` are therefore
the ``.sgo``'s own, key dialects included.

Base64 is 7-bit, so a ``.sgm`` survives a text conversion that wrecks a binary
``.sgo`` (the 03L906022LS and 03L906022QF ``.sgo`` files have every 0x81, 0x8D,
0x8F, 0x90 and 0x9D byte replaced by ``?``); their ``.sgm`` decodes.

Not written: nothing here builds a ``.sgm``, and a parsed container cannot be
written back as an ``.sgo`` (the section table and metadata block are not kept).
'''

import base64
import binascii
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import sgo as _sgo

_SIG = b"<SW-CNT>"
#: only the head is looked at to decide
_HEAD = 4096


def detect(data: bytes) -> bool:
    """True when ``data`` starts like a SW-CNT document."""
    head = bytes(data[:_HEAD])
    return b"SW-CNT" in head and (b"<!DOCTYPE SW-CNT" in head or _SIG in head)


def _hex(text: Optional[str]) -> Optional[int]:
    t = (text or "").strip()
    if not t:
        return None
    try:
        return int(t, 16)
    except ValueError:
        return None


@dataclass
class SgmBlock:
    """One ``DATENBLOCK``: what the XML says about it (the decoded bytes are on
    the matching :class:`ovtsuite.sgo.SgoSection` of :attr:`SgmContainer.sgo`)."""
    name: str
    format_name: str
    start: int
    fmt: int
    declared_len: int
    erase: Optional[Tuple[int, int]]
    check: Optional[Tuple[int, int]]
    checksum: Optional[int]
    content_name: str = ""          # the MIME ``name=`` (``03L906022LS_3610.bc1``)
    problem: str = ""               # why the block could not be taken, if so


@dataclass
class SgmContainer:
    name: str
    ident: Dict[str, str]
    kwp: Dict[str, object]
    blocks: List[SgmBlock]
    #: the blocks as decoded sections (same order as ``blocks`` less any that
    #: had a ``problem``); ``sgo.image(container.sgo)`` assembles the flash image
    sgo: _sgo.SgoContainer
    flash_order: List[str] = field(default_factory=list)

    @property
    def sections(self) -> List[_sgo.SgoSection]:
        return self.sgo.sections

    @property
    def unrecognized(self) -> List[_sgo.SgoSection]:
        return self.sgo.unrecognized

    @property
    def sa2_tape(self) -> Optional[bytes]:
        t = self.kwp.get("sa2")
        return bytes(t) if t else None

    @property
    def part_number(self) -> str:
        return self.ident.get("part_number", "")

    @property
    def software(self) -> str:
        return self.ident.get("sw_short", "")


def _text(el: Optional[ET.Element], path: str) -> str:
    if el is None:
        return ""
    t = el.findtext(path)
    return (t or "").strip()


def _ident(root: ET.Element) -> Dict[str, str]:
    ide = root.find("./IDENT")
    out = {"file": _text(ide, "./CNT-DATEI"),
           "version_type": _text(ide, "./CNT-VERSION-TYP"),
           "version": _text(ide, "./CNT-VERSION-INHALT"),
           "sw_short": _text(ide, "./SW-VERSION-KURZ"),
           "sw_long": _text(ide, "./SW-VERSION-LANG")}
    for line in _text(ide, "./CNT-IDENT-TEXT").splitlines():
        if "\t" not in line:
            continue
        k, v = (x.strip() for x in line.split("\t", 1))
        key = {"Kundennummer": "part_number", "Bosch SW-TTNR": "bosch_sw",
               "Bosch HW-TTNR": "bosch_hw", "Checksum lin": "checksum"}.get(k, k)
        if v and v != "-":
            out[key] = v
    return out


def _kwp(root: ET.Element) -> Dict[str, object]:
    out: Dict[str, object] = {}
    ab = root.find("./ABLAEUFE/ABLAUF")
    if ab is None:
        return out
    out["sequence"] = _text(ab, "./ABLAUF-NAME")
    kw = ab.find("./KWP-2000")
    if kw is not None:
        out["baud"] = [(_text(b, "./BAUDRATE-TESTER"), _hex(_text(b, "./BAUDRATE-SG")))
                       for b in kw.findall("./BAUDRATEN-2000/BAUDRATE-2000")]
        out["target"] = _hex(_text(kw, "./KWP-2000-TGT"))
        out["timing"] = {c.tag.replace("KWP-2000-", ""): _hex(c.text)
                         for c in kw.findall("./KWP-2000-ACP/*")}
        tape = _text(kw, "./KWP-2000-SA2")
        if tape:
            try:
                out["sa2"] = bytes(int(x, 16) for x in tape.split(","))
            except ValueError:
                pass
    out["order"] = [(v.text or "").strip() for v in ab.findall("./DATEN-VERWEISE/DATEN-VERWEIS")]
    return out


def _stream(daten: str) -> bytes:
    """The bytes of a ``DATENBLOCK-DATEN`` MIME entity: the headers up to the
    first blank line, then the base64 body."""
    text = daten.replace("\r\n", "\n").strip("\n")
    head, sep, body = text.partition("\n\n")
    if not sep:
        raise ValueError("no MIME header/body separator")
    if "base64" not in head.lower():
        raise ValueError("unsupported Content-Transfer-Encoding")
    try:
        return base64.b64decode("".join(body.split()), validate=True)
    except (binascii.Error, ValueError) as ex:
        raise ValueError("bad base64: %s" % ex)


def _header(start, fmt, dlen, erase, check, checksum, slen) -> bytes:
    """The 25-byte ``.sgo`` section-table entry for a block."""
    e0, e1 = erase or (start, start + dlen - 1)
    c0, c1 = check or (start, start + dlen - 1)
    h = bytearray()
    for v in (start,):
        h += struct.pack(">I", v)[1:]
    h.append(fmt & 0xFF)
    h += struct.pack(">I", dlen)[1:]
    for v in (e0, e1, c0, c1):
        h += struct.pack(">I", v)[1:]
    h += struct.pack(">H", (checksum or 0) & 0xFFFF)
    h += struct.pack("<I", slen)
    return bytes(h)


def parse(data: bytes, name: str = "") -> SgmContainer:
    """Parse a ``.sgm`` and decode every block it can (self-verified, see
    :func:`ovtsuite.sgo.assemble`). A block whose XML or base64 is unusable is
    listed with a ``problem`` and left out of the sections; one that decodes to
    nothing the format's own checks accept is an unrecognized section, exactly
    as in an ``.sgo``."""
    if not detect(data):
        raise ValueError("not a SW-CNT (.sgm) document")
    text = bytes(data).decode("latin-1")
    if "<!ENTITY" in text:
        raise ValueError("refusing a document that declares entities")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as ex:
        raise ValueError("not well-formed XML: %s" % ex)
    blocks: List[SgmBlock] = []
    sections: List[_sgo.SgoSection] = []
    for el in root.findall("./DATEN/DATENBLOECKE/DATENBLOCK"):
        er = el.find("./LOESCH-BEREICH")
        ck = el.find("./DATENBLOCK-CHECK")
        erase = ((_hex(_text(er, "./START-ADR")), _hex(_text(er, "./END-ADR")))
                 if er is not None else None)
        check = ((_hex(_text(ck, "./START-ADR")), _hex(_text(ck, "./END-ADR")))
                 if ck is not None else None)
        daten = el.findtext("./DATENBLOCK-DATEN") or ""
        mime = re.search(r'name="([^"]*)"', daten[:400])
        b = SgmBlock(name=_text(el, "./DATENBLOCK-NAME"),
                     format_name=_text(el, "./DATENBLOCK-FORMAT-NAME"),
                     start=_hex(_text(el, "./START-ADR")) or 0,
                     fmt=_hex(_text(el, "./DATENBLOCK-FORMAT")) or 0,
                     declared_len=_hex(_text(el, "./GROESSE-DEKOMPRIMIERT")) or 0,
                     erase=erase if erase and None not in erase else None,
                     check=check if check and None not in check else None,
                     checksum=_hex(_text(ck, "./CHECKSUMME")),
                     content_name=mime.group(1) if mime else "")
        blocks.append(b)
        try:
            raw = _stream(daten)
        except ValueError as ex:
            b.problem = str(ex)
            continue
        blob = _sgo._xorff(raw)           # the .sgo stores it under XOR 0xFF
        hdr = _header(b.start, b.fmt, b.declared_len, b.erase, b.check,
                      b.checksum, len(blob))
        sections.append(_sgo.SgoSection(
            index=len(sections), off=0, addr=b.start, crypt=b.fmt,
            declared_len=b.declared_len, header=hdr, blob=blob,
            lo=(b.check or (b.start, 0))[0],
            hi=(b.check or (0, b.start + b.declared_len - 1))[1]))
    kwp = _kwp(root)
    return SgmContainer(name=name, ident=_ident(root), kwp=kwp, blocks=blocks,
                        sgo=_sgo.assemble(sections, name=name),
                        flash_order=list(kwp.get("order", [])))


def image(container: SgmContainer, fill: int = 0xFF, max_size: int = _sgo.MAIN_IMAGE_MAX):
    """The flat flash image of the container (``(0, data)``), placed as
    :func:`ovtsuite.sgo.image` places the same areas."""
    return _sgo.image(container.sgo, fill, max_size)
