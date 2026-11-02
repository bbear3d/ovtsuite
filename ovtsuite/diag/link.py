# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Qt-free link factory: an interface choice + a transport + a module's
addressing -> a link the session layer drives.

The suite already has every link (serial / FTDI K-line, K-line over J2534,
TP 2.0 over a J2534 or USB OpenPort CAN channel, the KW-Dash remote); what
the logger does inline in its worker is gathered here so the diag session,
the CLI and the tests build links the same way without a dialog.

An :class:`Interface` is one entry of the picker: a serial port name, a
J2534 device record, the USB OpenPort marker, or a paired head unit. The
same entry can carry K-line or CAN depending on ``transport``.
"""
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from ..flash import j2534 as _j
from ..flash.transport import KLine

OPENPORT_USB = "openport-usb"
Log = Callable[[str], None]


@dataclass
class Interface:
    data: Any                     # port string | j2534 Device | OPENPORT_USB | head unit
    label: str
    kind: str                     # "serial" | "j2534" | "openport" | "remote"

    @property
    def can_capable(self) -> bool:
        return self.kind in ("j2534", "openport", "remote")

    @property
    def kline_capable(self) -> bool:
        return True                 # every entry: serial directly, J2534/OpenPort in hardware, remote via the car-side cable


def interfaces() -> List[Interface]:
    """Every usable interface, serial cables first, then J2534 devices, the
    USB OpenPort, and paired head units."""
    out: List[Interface] = []
    try:
        from ..flash import ftdi
        for dev, label in ftdi.list_ftdi_ports():
            out.append(Interface(dev, label, "serial"))
    except Exception:
        pass
    try:
        from ..flash import ports as _ports
        for p in _ports.cable_ports():
            label = p.device
            if p.description and p.description != "n/a":
                label = "%s - %s" % (p.device, p.description)
            out.append(Interface(p.device, label, "serial"))
    except Exception:
        pass
    try:
        for d in _j.list_devices():
            if _j.usable(d):
                out.append(Interface(d, d.label + ("" if d.loadable else "  (via 32-bit bridge)"), "j2534"))
    except Exception:
        pass
    try:
        from ..flash import openport
        for label in openport.find_devices():
            out.append(Interface(OPENPORT_USB, label + " (USB direct)", "openport"))
    except Exception:
        pass
    try:
        from ..flash import headunits
        for u in headunits.load():
            out.append(Interface(u, u.label, "remote"))
    except Exception:
        pass
    return out


def interface_from_data(data: Any) -> Interface:
    if data == OPENPORT_USB:
        return Interface(data, "OpenPort 2.0 (USB direct)", "openport")
    if getattr(data, "dll", None) is not None:
        return Interface(data, getattr(data, "label", str(data)), "j2534")
    if getattr(data, "host", None) is not None:
        return Interface(data, getattr(data, "label", str(data)), "remote")
    return Interface(str(data), str(data), "serial")


class Link:
    """A built link plus whatever has to be closed after it (the J2534
    device, the CAN channel). ``k`` presents the K-line facade the session
    drives (``wake_up`` / ``read`` / ``write_*`` / ``set_baud`` / ``close``)."""

    def __init__(self, k, owners=(), transport="kline", description=""):
        self.k = k
        self._owners = list(owners)
        self.transport = transport
        self.description = description

    def close(self):
        try:
            self.k.close()
        except Exception:
            pass
        for o in reversed(self._owners):
            _close_owner(o)


def _close_owner(o):
    """End an owner: a J2534 / OpenPort channel has ``disconnect()``, a
    device, a mux port or a client ``close()``."""
    fn = getattr(o, "close", None) or getattr(o, "disconnect", None)
    try:
        if fn is not None:
            fn()
    except Exception:
        pass


def close_owners(owners) -> None:
    """Close what :func:`open_obd` handed back, in reverse order."""
    for o in reversed(list(owners)):
        _close_owner(o)


def _closing(dev):
    """Close ``dev`` if the body raises, then re-raise: a J2534 device left
    open after a failed connect answers ERR_DEVICE_IN_USE until the process
    exits."""
    return _Closing(dev)


class _Closing:
    def __init__(self, dev):
        self.dev = dev

    def __enter__(self):
        return self.dev

    def __exit__(self, et, ev, tb):
        if et is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        return False


def open_device(iface: Interface):
    """The J2534-faced device behind a CAN-capable interface."""
    if iface.kind == "openport":
        from ..flash.openport import OpenPort
        return OpenPort()
    if iface.kind == "j2534":
        return _j.open_device(iface.data)
    raise ValueError("%s is not a CAN device" % iface.label)


class Bus:
    """One CAN connection kept open for several TP 2.0 channels at once: the
    device, its CAN channel, and the :class:`~.canmux.CanMux` that shares the
    channel between them. :meth:`link_for` builds a module's link on it; the
    links close their own channel only, :meth:`close` ends the connection."""

    def __init__(self, dev, channel, label: str = "", rate: int = 500000, log: Optional[Log] = None):
        from .canmux import CanMux, ListenIds
        self.dev = dev
        self.channel = channel
        self.label = label
        self.rate = rate
        self._log = log or (lambda s: None)
        self.mux = CanMux(channel, self._log)
        self.listen_ids = ListenIds()
        self.transport = "can"

    @classmethod
    def from_channel(cls, channel, label: str = "mock bus", log: Optional[Log] = None) -> "Bus":
        """A bus over an already-open CAN channel object (tests, replay)."""
        return cls(None, channel, label, 0, log)

    def link_for(self, module: dict, log: Optional[Log] = None, setup_timeout_ms: Optional[int] = None) -> Link:
        """A module's TP 2.0 link on this bus (its own listen id, its own
        port), closable without touching the connection. ``setup_timeout_ms``
        shortens the channel-setup wait (a scan visiting modules that may not
        be there; a fitted module answers the setup within milliseconds)."""
        from ..flash.tp20_transport import CanKLine, Tp20Channel
        log = log or self._log
        tp20 = module.get("tp20") or {}
        if "dest" not in tp20:
            raise ValueError("module %s has no TP 2.0 address in its definitions" % module.get("addr"))
        dest = _int(tp20["dest"])
        listen = self.listen_ids.take()
        port = self.mux.subscribe("%s (dest 0x%02X)" % (module.get("addr", "?"), dest))
        tp = Tp20Channel(port, dest=dest, listen_id=listen, log=log)
        if setup_timeout_ms:
            tp.poll_timeout_ms = int(setup_timeout_ms)
        k = CanKLine(tp=tp, dest=dest, log=log)
        return Link(k, [_PortReleaser(self, port, listen)], "can",
                    "TP 2.0 dest 0x%02X (listen 0x%03X) on %s" % (dest, listen, self.label))

    def close(self):
        try:
            self.mux.close()
        except Exception:
            pass
        for o in (self.channel, self.dev):
            try:
                if o is not None:
                    o.close()
            except Exception:
                pass


class _PortReleaser:
    def __init__(self, bus: Bus, port, listen: int):
        self.bus, self.port, self.listen = bus, port, listen

    def close(self):
        try:
            self.port.close()
        finally:
            if self.listen is not None:
                self.bus.listen_ids.give(self.listen)


def open_bus(iface: Interface, can_rate: int = 500000, log: Optional[Log] = None) -> Bus:
    """Open ``iface`` as a shared CAN bus (J2534 device or USB OpenPort; the
    32-bit bridge's channel works too, frame by frame). A paired head unit
    carries one TP 2.0 channel at a time and cannot be a bus."""
    log = log or (lambda s: None)
    if iface.kind == "remote":
        raise ValueError("%s carries one TP 2.0 channel at a time; parallel channels need a local CAN device"
                         % iface.label)
    dev = open_device(iface)
    try:
        ch = dev.connect(_j.CAN, can_rate)
        ch.pass_all()
        ch.clear_rx()
    except Exception:
        try:
            dev.close()
        except Exception:
            pass
        raise
    log("CAN bus open on %s at %d bit/s (shared by the TP 2.0 channels)" % (iface.label, can_rate))
    return Bus(dev, ch, iface.label, can_rate, log)


def open_link(iface: Interface, transport: str, module: dict, log: Optional[Log] = None,
              can_rate: int = 500000, fast_init: bool = False, kw1281: bool = False,
              bus: Optional[Bus] = None) -> Link:
    """Build the link for ``module`` (an addressing-table row) over
    ``transport`` ("kline" | "can") on ``iface``.

    K-line on a serial cable is :class:`KLine`; on a J2534 device it is the
    message link (KWP2000) or the byte-stream link (KW1281, which needs the
    device's own 5-baud init and acknowledges every byte); CAN is a TP 2.0
    channel at the module's logical address presented as a K-line
    (:class:`CanKLine`), locally or inside the 32-bit bridge. A paired head
    unit gives the same two faces over the network. With ``bus`` (an open
    :class:`Bus`) a CAN link is one more channel on that connection.
    """
    log = log or (lambda s: None)
    transport = transport or "kline"
    if transport == "uds":
        return _open_uds_link(iface, module, log, can_rate, bus)
    if transport == "can":
        if bus is not None:
            return bus.link_for(module, log)
        tp20 = module.get("tp20") or {}
        if "dest" not in tp20:
            raise ValueError("module %s has no TP 2.0 address in its definitions" % module.get("addr"))
        dest = _int(tp20["dest"])
        if iface.kind == "remote":
            from ..flash.remote import RemoteTp20
            from ..flash.tp20_transport import CanKLine
            client = _remote_client(iface, log)
            tp = RemoteTp20(client, dest=dest, rate=can_rate, log=log)
            return Link(CanKLine(tp=tp, dest=dest, log=log), [_Releaser(client)], "can",
                        "TP 2.0 dest 0x%02X via %s" % (dest, iface.label))
        dev = open_device(iface)
        from ..flash.tp20_transport import CanKLine
        with _closing(dev):
            if isinstance(dev, _j.BridgePassThru):
                tp = dev.open_tp20(dest=dest, baud=can_rate, params={"block_size": 0x0F})
                k = CanKLine(tp=tp, dest=dest, log=log)
            else:
                ch = dev.connect(_j.CAN, can_rate)
                ch.pass_all()
                ch.clear_rx()
                k = CanKLine(ch, dest=dest, log=log)
        return Link(k, [dev], "can", "TP 2.0 dest 0x%02X on %s" % (dest, iface.label))
    # ---- K-line
    if iface.kind == "remote":
        from ..flash.remote import RemoteKLine
        client = _remote_client(iface, log)
        k = RemoteKLine(client, baud=10400, prefer_fast_init=fast_init and not kw1281, log=log)
        return Link(k, [_Releaser(client)], "kline", "K-line via %s" % iface.label)
    if iface.kind in ("j2534", "openport"):
        dev = open_device(iface)
        with _closing(dev):
            if fast_init and not kw1281:
                # a fast init is KWP2000 by definition: the whole-frame link
                from ..flash.kline_j2534 import J2534KLine
                k = J2534KLine(dev, log=log, fast_init=True)
            else:
                # The protocol is only known from the key bytes after the
                # 5-baud init, and only the byte-stream link can continue in
                # KW1281 (every byte acknowledged by its complement) as well
                # as KWP2000. J2534KLine would die at the first KW1281 block,
                # and its slow init sends the address without the odd parity
                # the 5-baud convention wants.
                from ..flash.kline_j2534_raw import J2534RawKLine
                from ..flash.diag import EDC15_BAUD
                k = J2534RawKLine(dev, baud=EDC15_BAUD if kw1281 else 10400, log=log)
        return Link(k, [dev], "kline", "K-line on %s" % iface.label)
    k = KLine(str(iface.data), baud=10400, prefer_fast_init=fast_init and not kw1281)
    return Link(k, [], "kline", "K-line on %s" % iface.label)


def _open_uds_link(iface: Interface, module: dict, log: Log, can_rate: int, bus: Optional["Bus"]) -> Link:
    """A UDS-over-ISO-TP link for ``module`` (its ``isotp`` request/response
    ids). On a J2534 device the DLL's ISO15765 channel handles the framing;
    otherwise (OpenPort, or a shared bus) the suite's software ISO-TP runs on
    a raw CAN channel / mux port. The link's ``.k`` is a :class:`UdsLink`."""
    from ..flash.isotp_transport import IsoTp
    from .uds import UdsLink
    isotp = module.get("isotp") or {}
    if "request" not in isotp or "response" not in isotp:
        raise ValueError("module %s has no ISO-TP ids (isotp.request/response) in its definitions" % module.get("addr"))
    tx, rx = _int(isotp["request"]), _int(isotp["response"])
    if bus is not None:
        # only our response id: a pass-all port nobody drains between requests
        # would queue every broadcast frame for as long as the window is open
        port = bus.mux.subscribe("UDS %s (0x%03X)" % (module.get("addr", "?"), rx))
        port.pass_ids([rx])
        io = IsoTp(port, tx_id=tx, rx_id=rx, log=log)
        return Link(UdsLink(io, log=log), [_PortReleaser(bus, port, None)], "uds",
                    "UDS 0x%03X<-0x%03X on %s" % (tx, rx, bus.label))
    if iface.kind == "j2534":
        from ..flash.zf6hp_uds import connect_iso15765
        dev = open_device(iface)
        with _closing(dev):
            try:
                io, ch = connect_iso15765(dev, can_rate, tx_id=tx, rx_id=rx, log=log)
                return Link(UdsLink(io, [ch, dev], log=log), [], "uds",
                            "UDS 0x%03X<-0x%03X (ISO15765) on %s" % (tx, rx, iface.label))
            except Exception:
                ch = dev.connect(_j.CAN, can_rate)
                ch.pass_all()
                ch.clear_rx()
                io = IsoTp(ch, tx_id=tx, rx_id=rx, log=log)
                io.pass_ids()          # a pass-all J2534 channel overflows when nobody reads it
                return Link(UdsLink(io, [ch, dev], log=log), [], "uds",
                            "UDS 0x%03X<-0x%03X (raw CAN) on %s" % (tx, rx, iface.label))
    if iface.kind == "openport":
        dev = open_device(iface)
        with _closing(dev):
            ch = dev.connect(_j.CAN, can_rate)
            ch.pass_all()
            ch.clear_rx()
            io = IsoTp(ch, tx_id=tx, rx_id=rx, log=log)
            io.pass_ids()
        return Link(UdsLink(io, [ch, dev], log=log), [], "uds",
                    "UDS 0x%03X<-0x%03X on %s" % (tx, rx, iface.label))
    if iface.kind == "remote":
        raise ValueError("UDS needs a local CAN device (J2534 or OpenPort); %s carries TP 2.0 and K-line only"
                         % iface.label)
    raise ValueError("UDS needs a CAN device (J2534 or OpenPort); %s is K-line only" % iface.label)


def _remote_client(iface: Interface, log: Log):
    """A connected, claimed pass-through client for a paired head unit."""
    from ..flash import headunits
    client = headunits.connect(iface.data)
    client.claim()
    log("Pass-through via %s" % iface.label)
    return client


class _Releaser:
    """Closes a head-unit client the way the flasher does: release the
    claim, then the connection."""

    def __init__(self, client):
        self.client = client

    def close(self):
        for fn in ("release", "close"):
            try:
                getattr(self.client, fn)()
            except Exception:
                pass


def _int(v: Any) -> int:
    if isinstance(v, str):
        return int(v, 0)
    return int(v)


def open_obd(iface: Interface, transport: str, log: Optional[Log] = None, can_rate: int = 500000,
             bus: Optional[Bus] = None, fast_init: bool = False):
    """A generic OBD-II transport (:mod:`.obd`) on ``iface``: over CAN the
    ISO 15765-4 functional request on the shared bus when one is open (a
    pass-all port beside the TP 2.0 channels) or on a fresh CAN channel;
    over K-line the ISO 9141-2 / 14230-4 init at 0x33 on the byte-stream
    facade (serial cable, or the J2534 device's own 5-baud init). Returns
    ``(transport, owners)``; close the owners after the transport."""
    from .obd import ObdCan, ObdKLine, RESP_IDS
    log = log or (lambda s: None)
    if transport == "can":
        if bus is not None:
            port = bus.mux.subscribe("OBD-II")
            port.pass_ids(list(RESP_IDS))      # the emissions modules' answers, not the whole bus
            return ObdCan(port, log), [port]
        if iface.kind == "remote":
            raise ValueError("generic OBD-II over CAN needs a local CAN device (the head unit carries TP 2.0 only)")
        dev = open_device(iface)
        with _closing(dev):
            ch = dev.connect(_j.CAN, can_rate)
            ch.pass_all()
            ch.clear_rx()
            obd = ObdCan(ch, log)
        return obd, [ch, dev]
    if iface.kind == "remote":
        from ..flash.remote import RemoteKLine
        client = _remote_client(iface, log)
        k = RemoteKLine(client, baud=10400, prefer_fast_init=fast_init, log=log)
        return ObdKLine(k, log, fast_init=fast_init), [k, _Releaser(client)]
    if iface.kind in ("j2534", "openport"):
        from ..flash.kline_j2534_raw import J2534RawKLine
        dev = open_device(iface)
        with _closing(dev):
            k = J2534RawKLine(dev, baud=10400, log=log)
        return ObdKLine(k, log, fast_init=fast_init), [k, dev]
    k = KLine(str(iface.data), baud=10400, prefer_fast_init=fast_init)
    return ObdKLine(k, log, fast_init=fast_init), [k]
