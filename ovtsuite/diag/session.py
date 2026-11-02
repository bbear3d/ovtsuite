# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One control module, one session: identify it, read and clear its faults,
read its measuring groups -- over whatever link :mod:`.link` built.

:class:`ModuleSession` is the suite's :class:`ovtsuite.flash.diag.DiagSession`
(slow init, KWP2000 session, ``1A 9B``, ``21`` groups, ``18``/``14`` faults,
KW1281 identification/groups/faults, TP 2.0 via ``CanKLine``) with the
module-specific constants taken from the definitions instead of being the
engine's: the 5-baud address, the KWP2000 header address, the session byte,
whether the module is a K-line KW1281 unit. The protocol is decided by the
module's own wake-up keyword, never assumed: a KW1281 keyword continues in
KW1281, a KWP2000 keyword opens a KWP2000 session.

Qt-free; driven from one worker thread.
"""
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..flash import diag as _diag
from ..flash import kwp as _kwp
from ..flash.transport import KLineError, KLineTimeout, LinkStopped
from .defs import Identity, Resolver, as_int
from .link import Link
from .model import Fault, ModuleInfo

Log = Callable[[str], None]

_PART_RE = re.compile(r"^[0-9A-Z]{3}[0-9]{6}[A-Z]{0,2}$")
#: 5-baud rates to try on K-line, in order: the KWP2000 rate most modules
#: answer at, the 9600 of the KW1281 engine family (EDC15, VE pump), and the
#: 4800 of older body and ABS modules. A module's address-table row may name
#: its rate (``kline.baud``); that one is tried first and the others still
#: follow, so a wrong hint costs one attempt rather than the module.
KLINE_BAUDS = (10400, 9600, 4800)
#: an EDC16 re-initialises after a 0x9A coding write; wait before talking again
CODING_SETTLE_S = 3.0
#: identification data identifiers as the C6 gateway / body modules serve
#: them (Bear's autoscan capture); a module document may override by name
DEFAULT_DIDS = {"part": 0xF187, "sw": 0xF189, "hw_part": 0xF191, "hw_version": 0xF1A3,
                "system_name": 0xF197, "wsc_field": 0xF1A5, "coding": 0x0600}


class ModuleSession(_diag.DiagSession):
    def __init__(self, link: Link, module: Dict[str, Any], resolver: Optional[Resolver] = None,
                 log: Optional[Log] = None, generation: str = ""):
        super().__init__(link.k, ecu="GENERIC", log=log, fast_kw1281=True)
        self.link = link
        self.module = module                # the addressing-table row
        self.resolver = resolver
        self.generation = generation or ""
        self.addr = module.get("addr", "00")
        self.info = ModuleInfo(addr=self.addr, name=module.get("name", ""), transport=link.transport)
        self.protocol = ""
        self.uds = None                     # the UDS client once connected over ISO-TP
        self.moddef: Dict[str, Any] = {}    # the resolved module document once the protocol is known

    # --------------------------------------------------------------- connect
    def connect(self, speed="Low", cap_high=False, is_stop=None, probe=False):
        """Open the session and identify the module. ``probe``: a scan
        visiting modules that may not be fitted -- one 5-baud attempt per
        rate instead of three, so an absent address costs seconds, not a
        minute."""
        self._speed, self._cap = speed, cap_high
        self.kwp = self.kw = None
        self.uds = None
        self._arm_stop(is_stop)
        if self.link.transport == "uds":
            self._connect_uds(is_stop)
            self._identify_uds()
            return self.baud
        if self.link.transport == "can":
            self._connect_can(is_stop)
        else:
            self._connect_kline(is_stop, probe=probe)
        self._identify()
        return self.baud

    def _connect_uds(self, is_stop):
        """UDS over ISO-TP: open an extended diagnostic session on the link
        the UDS transport built; the session byte is the module document's
        ``uds.session`` (0x03 extended by default; a few want 0x4F)."""
        from .uds import UdsClient, SESSION_EXTENDED
        self.protocol = "uds"
        self.info.protocol = self.protocol
        self.baud = 0
        self._resolve_moddef()
        ident = self.info.identity.with_(generation=self.generation or None)
        self.uds = UdsClient(self.link.k, resolver=self.resolver, ident=ident, log=self.log)
        session = as_int((self.moddef.get("uds") or {}).get("session"), SESSION_EXTENDED)
        self._check_stop(is_stop)
        try:
            self.uds.open_session(session)
            self.log("UDS session open (10 %02X)." % session)
        except Exception as ex:
            self.log("UDS extended session refused (%s); staying in default." % ex)

    def _identify_uds(self):
        info = self.info
        info.reached = True
        try:
            fields = self.uds.identity()
        except Exception as ex:
            self.log("UDS identification: %s" % ex)
            fields = {}
        info.part = fields.get("part", "")
        info.sw = fields.get("sw", "")
        info.wsc = fields.get("wsc")
        for k in ("hw_part", "hw_version", "system_name", "importer", "equipment", "vin"):
            if fields.get(k) is not None:
                info.extra[k] = fields[k]
        try:
            cod = self.uds.read_coding()
            info.coding_bytes = cod.hex()
            info.coding = int.from_bytes(cod, "big") if 0 < len(cod) <= 3 else None
        except Exception:
            pass
        parts = [p for p in (info.part, ("SW " + info.sw) if info.sw else "",
                             ("HW %s" % info.extra.get("hw_version")) if info.extra.get("hw_version") else "",
                             info.extra.get("system_name", "")) if p]
        self.identity = info.identity_text = "  ".join(parts)
        info.extra["identification"] = "uds_dids"
        if not info.platform:
            info.platform = platform_of(info.part, "uds") or ""
        self._namer = None
        self._resolve_moddef()

    def _resolve_moddef(self):
        if self.resolver is None:
            self.moddef = {}
            return
        self.moddef = self.resolver.module(self.info.identity.with_(generation=self.generation or None))

    def _connect_can(self, is_stop):
        """KWP2000 inside TP 2.0: the channel is the wake-up; the session
        byte comes from the module document (0x89 default; the TCUs take
        0x86)."""
        self.protocol = "kwp2000"
        self.info.protocol = self.protocol
        self._resolve_moddef()
        self.baud = 0
        tp = getattr(self.k, "tp", None)
        if tp is not None and not getattr(tp, "opened", True):
            self.log("Opening the TP 2.0 channel to %s (%s)..." % (self.addr, self.info.name))
            self._check_stop(is_stop)
            self.k.wake_up()
        self._open_kwp_session(is_stop)

    def kline_bauds(self) -> List[int]:
        """The rates to try for this module, the address-table hint first."""
        kline = self.module.get("kline") or {}
        hint = as_int(kline.get("baud"), None) if kline.get("baud") else None
        out = [hint] if hint else []
        out += [b for b in KLINE_BAUDS if b != hint]
        return out

    def _connect_kline(self, is_stop, probe: bool = False):
        kline = self.module.get("kline") or {}
        address = as_int(kline.get("address"), None)
        if address is None:
            address = int(self.addr, 16)
        bauds = self.kline_bauds()
        tries = 1 if probe else 3
        last_error: Optional[Exception] = None
        version = None
        for baud in bauds:
            self._check_stop(is_stop)
            try:
                self.k.set_baud(baud)
            except Exception:
                pass
            self.log("Connecting to %s (%s): 5-baud init at address 0x%02X, %d baud..."
                     % (self.addr, self.info.name, address, baud))
            try:
                try:
                    version = self.k.wake_up(address, even_parity=False, tries=tries)
                except TypeError:                   # a link without the tries argument
                    version = self.k.wake_up(address, even_parity=False)
            except LinkStopped:
                raise
            except (KLineError, KLineTimeout) as ex:
                last_error = ex
                self.log("  no answer at %d baud (%s)" % (baud, ex))
                continue
            self.baud = baud
            break
        if version is None:
            raise KLineError("No answer from module %s (%s) at %s baud: %s"
                             % (self.addr, self.info.name, " / ".join(str(b) for b in bauds), last_error))
        self.info.extra["baud"] = self.baud
        if version >= 2000:
            self.protocol = "kwp2000"
            self.info.protocol = self.protocol
            self._resolve_moddef()
            self._open_kwp_session(is_stop)
            self._maybe_raise_baud()
            self.info.extra["baud"] = self.baud
        else:
            self.protocol = "kw1281"
            self.info.protocol = self.protocol
            self._resolve_moddef()
            self.log("KW1281 keyword (KW %d) -- continuing in KW1281 at %d." % (version, self.baud))
            self._continue_kw1281(self.baud)

    def _open_kwp_session(self, is_stop):
        kwpdef = self.moddef.get("kwp") or {}
        if self.link.transport == "can":
            # CAN carries no K-line addressing: CanKLine reframes every reply
            # as src 0x10 whatever the module, so the dialog checks for that
            header = 0x10
        else:
            header = as_int(self.module.get("kwp_header"), None)
            if header is None:
                header = as_int(kwpdef.get("header"), None)
            if header is None:
                header = int(self.addr, 16)
        session = as_int(kwpdef.get("session"), 0x89)
        d = self._new_kwp(header)
        d.P3, d.P4 = 0.025, 0.0
        d.fast = True
        self.kwp = d
        time.sleep(0.05)
        self._check_stop(is_stop)
        d.send_receive(0x10, bytes([session & 0xFF]))
        self.log("Diagnostic session open (10 %02X), header 0x%02X." % (session, header))

    def _maybe_raise_baud(self):
        """A link-speed raise only when the module's definition lists speed
        bytes and the caller asked for more than Low."""
        speeds = (self.moddef.get("variants") or {}).get("link_speed")
        if not speeds or self._speed == "Low":
            return
        try:
            self._raise_baud(self._speed, self._cap)
        except (KLineError, KLineTimeout) as ex:
            self.log("Link speed raise refused (%s); staying at %d." % (ex, self.baud))

    # -------------------------------------------------------------- identity
    def _identify(self):
        info = self.info
        info.reached = True
        if self.kwp is not None:
            record = (self.moddef.get("identification") or {}).get("record")
            rec = b""
            refused = record == "kwp_dids"      # the definitions say: identifiers, not the 9B record
            if not refused:
                try:
                    rec = bytes(self.kwp.send_receive(0x1A, b"\x9B").body)
                except LinkStopped:
                    raise
                except _kwp.NegativeResponse as ex:
                    self.log("readEcuIdentification 9B refused (%s)" % ex)
                    refused = True
                except (KLineError, KLineTimeout) as ex:
                    self.log("readEcuIdentification 9B: %s" % ex)
            if rec:
                self.identity = _diag.format_identity(rec)
                fields = parse_9b(rec)
                info.part, info.sw = fields.get("part", ""), fields.get("sw", "")
                info.coding, info.wsc = fields.get("coding"), fields.get("wsc")
                info.coding_bytes = ""
                info.extra.update({k: v for k, v in fields.items() if k in ("importer", "equipment")})
                info.identity_text = self.identity
            elif refused:
                # the module says it has no 9B record: the C6 body modules and
                # the gateway identify through data identifiers instead
                self._identify_dids()
            else:
                self.identity = ""
                info.identity_text = ""
        else:
            info.identity_text = self.identity
            info.coding, info.wsc = self.coding, self.workshop_code
            tok = (self.identity.split() or [""])[0].upper()
            if _PART_RE.match(tok):
                info.part = tok
        if info.identity_text:
            self.log("%s %s: %s" % (self.addr, info.name, info.identity_text))
        if not info.platform:
            info.platform = platform_of(info.part, self.protocol) or ""
            if info.platform:
                self.log("Platform %s (part number in the factory catalogue)" % info.platform.upper())
        self._namer = None
        # the identity may change what applies (part-specific documents)
        self._resolve_moddef()

    def _identify_dids(self):
        """Identification from data identifiers (``22 F1 xx`` ...): the form
        the C6 gateway and body modules serve (Bear's autoscan capture). The
        identifier numbers come from the module document's
        ``identification.dids`` when it names them, else the captured set."""
        from .kwp2000 import Wsc
        info = self.info
        dids = dict(DEFAULT_DIDS)
        for name, did in ((self.moddef.get("identification") or {}).get("dids") or {}).items():
            v = as_int(did, None)
            if v is not None:
                dids[str(name)] = v
        got = self.kwp2000.read_dids(dids)

        def text(key):
            return "".join(chr(c) for c in got.get(key, b"") if 0x20 <= c < 0x7F).strip()

        info.part = text("part").replace(" ", "")
        info.sw = text("sw")
        for key in ("hw_part", "hw_version", "system_name"):
            if key in got:
                info.extra[key] = text(key)
        coding = got.get("coding")
        if coding is not None:
            info.coding_bytes = coding.hex()
            info.coding = int.from_bytes(coding, "big") if len(coding) <= 3 else None
        wsc6 = got.get("wsc_field")
        if wsc6 is not None and len(wsc6) == 6:
            w = Wsc.decode(wsc6)
            info.wsc, info.extra["importer"], info.extra["equipment"] = w.wsc, w.importer, w.equipment
        parts = [p for p in (info.part, ("SW " + info.sw) if info.sw else "",
                             ("HW %s %s" % (info.extra.get("hw_part", ""), info.extra.get("hw_version", ""))).strip()
                             if info.extra.get("hw_part") or info.extra.get("hw_version") else "",
                             info.extra.get("system_name", "")) if p]
        self.identity = info.identity_text = "  ".join(parts)
        info.extra["identification"] = "dids"

    # ---------------------------------------------------------------- gateway
    def installation_list(self):
        """The gateway's installation list (:mod:`.gateway`): which modules
        the car has, at which TP 2.0 addresses, with their status."""
        from .gateway import DID_INSTALLATION_LIST, parse_installation_list
        if self.kwp is None:
            raise NotSupported("the installation list is a KWP2000 (TP 2.0) gateway function")
        self._check_variant("installation_list", "did_04a1")
        gw = self.module.get("gateway") or {}
        did = as_int(gw.get("installation_list_did"), DID_INSTALLATION_LIST)
        data = self.kwp2000.installation_list(did)
        names = {}
        if self.resolver is not None:
            names = {m["addr"]: m.get("name", "") for m in self.resolver.addressing(self.generation or None)}
        lst = parse_installation_list(data, names)
        lst.gateway_addr = self.addr
        self.info.extra["installation_list"] = lst.to_dict()
        self.log("Installation list: %d modules, %d installed" % (len(lst.entries), len(lst.installed)))
        return lst

    @property
    def ident(self) -> Identity:
        return self.info.identity.with_(generation=self.generation or None)

    # ---------------------------------------------------------------- faults
    def read_faults(self, with_freeze: Optional[bool] = None) -> List[Fault]:
        """The stored faults, decoded. ``with_freeze``: also fetch each
        fault's freeze frame (``12 00 04 <dtc>``) -- the default on a KWP2000
        module; a module that refuses simply yields none."""
        if self.uds is not None:
            out = []
            for d in self.uds.read_dtcs():
                out.append(Fault(code=int(d.code, 16) if d.code else 0, status=d.status, proto="uds",
                                 vag=None, sae=d.sae, text=d.text, status_text=d.status_info.get("text", ""),
                                 intermittent=not d.status_info.get("present", False)))
            self.info.faults = out
            return out
        out = []
        for d in self.read_dtcs():
            out.append(self._fault_from(d))
        if (with_freeze if with_freeze is not None else self.kwp is not None) and self.kwp is not None:
            for f in out:
                try:
                    cells = self.kwp2000.freeze_frame(f.code)
                except Exception as ex:
                    self.log("freeze frame of %s: %s" % (f.display_code, ex))
                    cells = []
                f.freeze = {"cell %d" % (i + 1): c.decoded[2] for i, c in enumerate(cells)}
                f.freeze_raw = ["%02X %02X %02X" % (c.fid, c.a, c.b) for c in cells]
        self.info.faults = out
        return out

    def clear_faults(self):
        if self.uds is not None:
            self.uds.clear_dtcs()
        else:
            self.clear_dtcs()
        self.info.faults = []

    @property
    def controller(self) -> str:
        """The controller family name ``dtc_report`` keys its per-family
        status decoding on (EDC15 / EDC16), from the definitions when they
        say, else empty (generic protocol decoding)."""
        plat = (self.moddef.get("platform") or self.info.platform or "")
        return str(plat).upper()

    def _fault_from(self, d: _diag.Dtc) -> Fault:
        from .. import dtc as _dtc
        from .. import dtc_report as _rep
        proto = "kw1281" if d.proto == _diag.DTC_KW1281 else "kwp2000"
        code = int(d.code)
        # how the word is encoded comes from the definitions (the module
        # document's faults.encoding), never from the word's magnitude:
        # VAG 01044 and SAE P0414 are both 0x0414
        vag, sae = decode_fault_word(code, proto, fault_word_encoding(self.moddef, self.controller, proto))
        text = ""
        if self.resolver is not None:
            text = self.resolver.fault_text(self.ident, vag=vag, sae=sae or None)
        if not text and sae:
            text = _dtc._load_pdesc().get(sae, "")
        # the status byte in words: OVT-Suite's own decoding (symptom, the
        # KWP2000 flag bits, intermittent), per controller family when known
        intermittent = False
        try:
            si = _rep.decode_status(int(d.status), self.controller, d.proto)
            status_text = si.text
            intermittent = bool(si.intermittent)
        except Exception:
            status_text = "0x%02X" % int(d.status)
        return Fault(code=code, status=int(d.status), proto=proto, vag=vag, sae=sae, text=text,
                     status_text=status_text, intermittent=intermittent)

    # ------------------------------------------------------------ live data
    @property
    def catalogue_namer(self):
        """The logger's catalogue namer for this module's identity (names from
        the factory calibration of its part number), when its platform has
        a catalogue; None otherwise."""
        if getattr(self, "_namer", None) is None:
            self._namer = False
            if self.info.platform:
                try:
                    from ..datalog import msbcatalogue as _cat
                    n = _cat.namer_for(self.info.platform.upper(), self.info.identity_text)
                    if n is not None and n.available:
                        self._namer = n
                except Exception:
                    self._namer = False
        return self._namer or None

    def group_names(self, group: int) -> List[Optional[str]]:
        """Cell names for a group: the definitions first, else the factory
        catalogue of the module's part number (the logger's namer)."""
        names: List[Optional[str]] = []
        if self.resolver is not None:
            names = list(self.resolver.measuring_names(self.ident).get(group, []))
        if names and any(names):
            return names
        namer = self.catalogue_namer
        if namer is None or group not in namer.populated_groups():
            return names
        out = []
        for f in range(namer.slots(group)):
            label = namer.resolve(group, f)[0]
            out.append(None if label.startswith("Group ") else label)
        return out

    def read_group_named(self, group: int):
        """``[(name or None, Cell)]`` for one measuring group, None when the
        module has no such group."""
        cells = self.read_group(group)
        if cells is None:
            return None
        names = self.group_names(group)
        return [(names[i] if i < len(names) else None, c) for i, c in enumerate(cells)]

    # ----------------------------------------------- UDS live data (P5)
    def uds_live_dids(self) -> List[Dict[str, Any]]:
        """The DIDs this module offers in Live data: those flagged ``live``
        in its ``dids`` definitions (id, did, name, unit)."""
        out = []
        if self.resolver is None or self.uds is None:
            return out
        for r in self.resolver.view("dids", self.ident).entries.values():
            e = r.entry
            if e.get("live") and as_int(e.get("did"), None) is not None:
                out.append({"id": e.get("id", ""), "did": as_int(e.get("did")), "name": e.get("name", ""),
                            "unit": e.get("unit", "")})
        return out

    def uds_read_dids(self, dids: List[int]):
        """``[DidReading]`` for the DIDs; a refused one is skipped."""
        from .uds import Refused
        out = []
        for d in dids:
            try:
                out.append(self.uds.read_did(int(d)))
            except Refused as ex:
                self.log("DID 0x%04X: %s" % (int(d), ex))
        return out

    def uds_routine(self, rid: int, data: bytes = b"", sub: int = 0x01) -> bytes:
        return self.uds.routine(int(rid), bytes(data), sub=sub)

    def read_long_coding(self) -> Optional[bytes]:
        if self.uds is not None:
            try:
                cod = self.uds.read_coding()
                self.info.coding_bytes = cod.hex()
                return cod
            except Exception as ex:
                self.log("long coding read: %s" % ex)
                return None
        if self.info.coding_bytes:
            return bytes.fromhex(self.info.coding_bytes)
        return None

    def write_long_coding(self, data: bytes) -> bool:
        """Write the long coding (UDS ``2E 06 00``) and re-identify, so the
        caller can compare what the module now reports. The payload must be
        the length of the coding the module holds (read first): a wrong-length
        write is how body modules are bricked, so it is refused here rather
        than left to the module. KWP2000 long coding is not written here (the
        0x9A record is the short form)."""
        if self.uds is None:
            raise NotSupported("long coding is written over UDS; this module is %s" % (self.protocol or "not connected"))
        from .uds import Refused
        data = bytes(data)
        if not data:
            raise ValueError("long coding: nothing to write")
        try:
            current = self.uds.read_coding()
        except Exception as ex:
            raise ValueError("long coding: the module's current coding could not be read (%s); not writing" % ex)
        if len(current) != len(data):
            raise ValueError("long coding: the module holds %d bytes, %d given; the length must not change"
                             % (len(current), len(data)))
        if data == current:
            self.log("long coding unchanged; nothing written")
            return True
        try:
            self.uds.write_coding(data)
        except Refused as ex:
            self.log("long coding write refused: %s" % ex)
            return False
        self._identify_uds()
        if self.info.coding_bytes.lower() != data.hex().lower():
            self.log("long coding: the module reports %s after the write, not %s"
                     % (self.info.coding_bytes.upper() or "nothing", data.hex().upper()))
        return True

    # ------------------------------------------- generic functions (P1 / P2)
    @property
    def kw1281(self):
        """The KW1281 command layer, once the module is connected in KW1281."""
        if self.kw is None:
            raise NotSupported("this module is connected in %s; KW1281 functions need KW1281"
                               % (self.protocol or "no protocol"))
        if getattr(self, "_cmds", None) is None or self._cmds.kw is not self.kw:
            from .kw1281 import Kw1281Commands
            self._cmds = Kw1281Commands(self.kw, self.log)
        return self._cmds

    @property
    def kwp2000(self):
        """The KWP2000 function layer, once the module is connected in KWP2000."""
        if self.kwp is None:
            raise NotSupported("this module is connected in %s; KWP2000 functions need KWP2000"
                               % (self.protocol or "no protocol"))
        if getattr(self, "_kcmds", None) is None or self._kcmds.d is not self.kwp:
            from .kwp2000 import Kwp2000Commands
            self._kcmds = Kwp2000Commands(self.kwp, self.log)
        return self._kcmds

    def variant(self, function: str) -> str:
        """How this module does ``function`` per its definitions (``variants``
        block); the protocol's own form when the definitions are silent."""
        v = (self.moddef.get("variants") or {}).get(function)
        if v:
            return str(v)
        if self.kw is not None:
            return "kw1281"
        if self.uds is not None:
            return {"coding": "uds_0600", "security_access": "uds_seed_key", "actuator_tests": "uds_io_control",
                    "adaptation": "uds_did"}.get(function, "")
        return {"adaptation": "routine_b8", "actuator_tests": "routine_b8", "basic_settings": "routine_b8",
                "coding": "record_9a", "login": "record_9a_bit23", "security_access": "seed_plus_code",
                "installation_list": "did_04a1"}.get(function, "")

    def _check_variant(self, function: str, *allowed: str):
        v = self.variant(function)
        if v not in allowed:
            raise NotSupported("%s on this module is %r, which OVT Diag does not implement yet" % (function, v))
        return v

    def _wsc(self, wsc: Optional[int]) -> int:
        if wsc is not None:
            return int(wsc)
        return int(self.info.wsc or 0)

    def _wsc6(self, wsc: Optional[int]):
        """The 6-byte KWP2000 workshop field: the module's own when ``wsc``
        is None (importer / equipment kept), else ``wsc`` with the module's
        importer and equipment."""
        from .kwp2000 import Wsc
        if wsc is None:
            return None
        return Wsc(int(wsc), int(self.info.extra.get("importer") or 0), int(self.info.extra.get("equipment") or 0))

    # ---- login and security access
    def login(self, code: int, wsc: Optional[int] = None) -> bool:
        if self.kw is not None:
            return self.kw1281.login(int(code), self._wsc(wsc))
        self._check_variant("login", "record_9a_bit23")
        try:
            self.kwp2000.login(int(code), self._wsc6(wsc))
        except Exception as ex:
            from .kwp2000 import Refused
            if isinstance(ex, Refused):
                self.log("Login refused: %s" % ex)
                return False
            raise
        return True

    def security_access(self, code: int = 0) -> bool:
        """KWP2000 security access (``27``, key = seed + code). On UDS the key
        comes from the named algorithm in the module's definitions (its flash
        path solved it), so ``code`` is unused there."""
        if self.uds is not None:
            self._check_variant("security_access", "uds_seed_key")
            sec = self.moddef.get("security") or {}
            alg = sec.get("key_algorithm") or (self.moddef.get("variants") or {}).get("key_algorithm")
            level = as_int(sec.get("security_level"), 3)
            levels = sec.get("key_levels") or {}
            if str(level) in levels:
                alg = levels[str(level)]
            if not alg:
                raise NotSupported("this module's definitions name no security key algorithm")
            return self.uds.security_access(str(alg), level=level)
        if self.kw is not None:
            raise NotSupported("KW1281 modules have no security access; use login")
        self._check_variant("security_access", "seed_plus_code")
        return self.kwp2000.security_access(int(code))

    # ---- adaptation
    def adaptation_read(self, channel: int):
        if self.kw is not None:
            return self.kw1281.adaptation_read(channel)
        self._check_variant("adaptation", "routine_b8")
        k = self.kwp2000
        k.adaptation_enter()
        try:
            k.adaptation_select(channel)
            return k.adaptation_read(channel)
        finally:
            k.adaptation_exit()

    def adaptation_test(self, channel: int, value: int):
        if self.kw is not None:
            return self.kw1281.adaptation_test(channel, value)
        self._check_variant("adaptation", "routine_b8")
        k = self.kwp2000
        k.adaptation_enter()
        try:
            k.adaptation_select(channel)
            return k.adaptation_test(channel, value)
        finally:
            k.adaptation_exit()

    def adaptation_save(self, channel: int, value: int, wsc: Optional[int] = None):
        if self.kw is not None:
            return self.kw1281.adaptation_save(channel, value, self._wsc(wsc))
        self._check_variant("adaptation", "routine_b8")
        k = self.kwp2000
        w6 = self._wsc6(wsc)
        wsc6 = w6.encode() if w6 is not None else k.record_fields()[0]
        k.adaptation_enter()
        try:
            k.adaptation_select(channel)
            return k.adaptation_save(channel, value, wsc6)
        finally:
            k.adaptation_exit()

    # ---- coding
    def set_coding(self, coding: int, wsc: Optional[int] = None):
        """Write a short coding; returns ``(accepted, coding, wsc)`` as the
        module reports them afterwards (its identification is re-read)."""
        if self.kw is not None:
            ok, blocks = self.kw1281.set_coding(int(coding), self._wsc(wsc))
            if ok and blocks:
                text, cod, w = _diag.kw1281_identity(blocks)
                if cod is not None:
                    self.info.coding, self.info.wsc = cod, w
                    self.coding, self.workshop_code = cod, w
                if text:
                    self.identity = self.info.identity_text = text
            return ok, self.info.coding, self.info.wsc
        self._check_variant("coding", "record_9a")
        from .kwp2000 import Refused
        try:
            self.kwp2000.set_coding(int(coding), self._wsc6(wsc))
        except Refused as ex:
            self.log("Coding refused: %s" % ex)
            return False, self.info.coding, self.info.wsc
        time.sleep(CODING_SETTLE_S)         # an EDC16 re-initialises after a coding write
        self._identify()
        return True, self.info.coding, self.info.wsc

    # ---- actuator tests
    def actuator_next(self):
        """KW1281: the next actuator of the sequence (one block). KWP2000:
        enter the routine on the first call, then advance; the module keeps
        the current actuator active until the next call or :meth:`actuator_stop`."""
        if self.kw is not None:
            return self.kw1281.actuator_next()
        self._check_variant("actuator_tests", "routine_b8")
        k = self.kwp2000
        if not getattr(self, "_act_open", False):
            k.actuator_enter()
            self._act_open = True
        else:
            k.actuator_advance()
        step = k.actuator_current()
        if step.ended:
            self.actuator_stop()
        return step

    def actuator_stop(self):
        if self.kwp is not None and getattr(self, "_act_open", False):
            self._act_open = False
            self.kwp2000.actuator_exit()

    # ---- basic settings
    def basic_setting_read(self, group: int):
        """One basic-setting read. KWP2000: the group is entered on the first
        read and stays entered until :meth:`basic_setting_stop` or another
        group is read."""
        from .kw1281 import GroupReading
        if self.kw is not None:
            return self.kw1281.read_group(group, basic=True)
        self._check_variant("basic_settings", "routine_b8")
        k = self.kwp2000
        from .kwp2000 import Refused
        if getattr(self, "_basic_group", None) != group:
            self.basic_setting_stop()
            try:
                k.basic_enter(group)
            except Refused:
                return GroupReading(group=group, available=False)
            self._basic_group = group
        try:
            cells = k.basic_read(group)
        except Refused:
            return GroupReading(group=group, available=False)
        return GroupReading(group=group, cells=cells, available=True)

    def basic_setting_stop(self):
        g = getattr(self, "_basic_group", None)
        if g is not None and self.kwp is not None:
            self._basic_group = None
            self.kwp2000.basic_exit(g)

    def read_group_full(self, group: int):
        """A group read with the raw/text forms kept (the P0
        :meth:`read_group` keeps only decoded cells)."""
        from .kw1281 import GroupReading
        if self.uds is not None:
            raise NotSupported("UDS modules have no numbered measuring groups; read their DIDs")
        if self.kw is not None:
            return self.kw1281.read_group(group, basic=False)
        cells = self.read_group(group)
        return GroupReading(group=group, cells=cells, available=cells is not None)

    def read_memory_kw(self, kind: str, start: int, length: int, is_stop=None) -> Optional[bytes]:
        if self.kw is not None:
            return self.kw1281.read_memory_range(kind, start, length, is_stop=is_stop)
        raise NotSupported("memory reads over KWP2000 use the flasher")

    # ---------------------------------------------------------------- close
    def close(self):
        if self.uds is not None:
            self.uds = None
            self.link.close()
            return
        try:
            self.actuator_stop()
            self.basic_setting_stop()
        except Exception:
            pass
        super().close()
        self.link.close()

    def keepalive(self):
        if self.uds is not None:
            self.uds.tester_present()
            return
        super().keepalive()


class NotSupported(Exception):
    """A function this module's protocol (or this phase of OVT Diag) does not offer."""


#: what ``ovtsuite.dtc_report`` knows of a controller family's fault-word
#: encoding when the module document is silent: every EDC15 reports the VAG
#: 5-digit number; an EDC16 does so or reports the SAE word by calibration
#: (P350/P379/P213/P216 VAG, P447/P475/late P529 SAE), which dtc_report takes
#: as VAG when not recorded -- a per-part module document says when it is SAE.
_CONTROLLER_FAULT_ENCODING = {"EDC15": "vag", "EDC16": "vag"}


def fault_word_encoding(moddef: Dict[str, Any], controller: str = "", protocol: str = "kwp2000") -> str:
    """How a module's reported fault word is encoded (``defs.schema
    .FAULT_ENCODINGS``: ``vag`` / ``sae``): KW1281 words are always VAG
    numbers; for KWP2000 the resolved module document's ``faults.encoding``
    decides, else the controller family's known encoding, else ``vag``."""
    if (protocol or "").lower() == "kw1281":
        return "vag"
    enc = str(((moddef or {}).get("faults") or {}).get("encoding") or "").strip().lower()
    if enc in ("vag", "sae"):
        return enc
    return _CONTROLLER_FAULT_ENCODING.get((controller or "").strip().upper(), "vag")


def decode_fault_word(code: int, protocol: str, encoding: str) -> Tuple[Optional[int], str]:
    """``(vag, sae)`` for a reported word under ``encoding``: a VAG number
    with the P-code it stands for (16683 -> P0299, 01044 -> none), or an SAE
    word with the VAG number of the same fault (0x0299 -> P0299, 16683)."""
    from .. import dtc as _dtc
    from .. import dtc_report as _rep
    code = int(code)
    if (protocol or "").lower() != "kw1281" and encoding == "sae":
        return _rep.vag5_of(code, _rep.ENC_SAE), _rep.pcode_of(code, _rep.ENC_SAE)
    return code, _dtc.pcode_of_vag5(code) or ""


def platform_of(part: str, protocol: str = "") -> Optional[str]:
    """The suite's platform id for a served part number, when that part is
    in a platform's factory measuring-block catalogue (``edc16``/``edc15``
    ``_msblocks.json.gz``) -- a structural match, nothing inferred from the
    number's shape. None when no catalogue lists it."""
    part = (part or "").upper().replace(" ", "")
    if not part:
        return None
    try:
        from ..datalog import msbcatalogue as _cat
    except Exception:
        return None
    order = ("edc15", "edc16") if protocol == "kw1281" else ("edc16", "edc15")
    for plat in order:
        try:
            cat = _cat.load(plat)
        except Exception:
            cat = None
        if cat is not None and part in getattr(cat, "_by_part", {}):
            return plat
    return None


def parse_9b(rec: bytes) -> Dict[str, Any]:
    """The fields of a ``readEcuIdentification 9B`` record: part number (12
    ASCII), software version (4 ASCII), 0x03, coding (3 bytes big-endian),
    the 6-byte packed workshop field (WSC in bits 0-16, importer 17-26,
    equipment 27-46, as KWHack decoded it from a VCDS capture), then text."""
    out: Dict[str, Any] = {}
    rec = bytes(rec or b"")
    if len(rec) < 17 or rec[0] != 0x9B:
        return out
    def ascii_(b):
        return "".join(chr(c) for c in b if 0x20 <= c < 0x7F).strip()
    out["part"] = ascii_(rec[1:13]).replace(" ", "")
    out["sw"] = ascii_(rec[13:17])
    if len(rec) >= 27 and rec[17] == 0x03:
        out["coding"] = int.from_bytes(rec[18:21], "big")
        packed = int.from_bytes(rec[21:27], "big")
        out["wsc"] = packed & 0x1FFFF
        out["importer"] = (packed >> 17) & 0x3FF
        out["equipment"] = (packed >> 27) & 0xFFFFF
        out["text"] = ascii_(rec[27:])
    return out
