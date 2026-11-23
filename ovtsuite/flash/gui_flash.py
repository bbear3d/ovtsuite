# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""GUI flashing: port/ECU/speed picker with a live stage label + progress bar,
driving the EDC16 or EDC15 read/write (and EDC15 EEPROM read/write) on a
worker thread (cancellable).

A flash read hands the image back to the main window (opened as an unsaved
capture); a flash write flashes the current image after checksum-correcting it.
EDC15 EEPROM reads are saved to a file of the user's choosing (with the decoded
VIN / immobiliser / SKC / odometer summary); EEPROM writes take a 512-byte file.
"""
import datetime
import os
import threading
import time
import traceback

from PySide6 import QtCore, QtWidgets

from . import image as _image
from . import edc16 as _edc16
from . import gui_edc16 as _gui16
from . import edc15 as _edc15
from . import edc15boot as _boot
from . import climatronic as _clim
from . import cluster as _clus
from . import abs_esp8 as _abs
from . import zf6hp as _zf6hp
from . import aisin09d as _aisin09d
from . import vdo_cluster as _vdo
from . import keepawake as _keepawake
from . import gui_dialogbase as _dlgbase
from .transport import KLine, KLineError
from .. import branding
from .. import checksum_memo as _memo
from .. import platforms as _platforms
from ..modules import api as _modapi
from ..modules import registry as _registry


class _WriteCancelled(Exception):
    """The user declined to write an image whose checksum cannot be
    corrected (its module is not installed). Not an error: nothing happens."""


def _is_link_failure(ex):
    """A transport-level failure (no answer, channel setup refused, the device
    could not send), as opposed to a bug in our own code."""
    if isinstance(ex, KLineError):
        return True
    try:
        from .j2534 import J2534Error
    except Exception:                       # noqa: BLE001 - no J2534 support here
        return False
    return isinstance(ex, J2534Error)


class _Tp20NoAnswer(Exception):
    """An Automatic ZF6HP read got no answer over TP 2.0: the channel setup
    failed or the TCU never answered a single KWP request. Only then may the
    read move on to UDS (6HP28); see _Worker._run_zf6hp."""

# Fixed CAN TP2.0 destination address per ECU. Each flasher module owns its own
# correct value (edc16.CAN_DEST, climatronic.DEST_HVAC, cluster.DEST_CLUSTER,
# zf6hp.DEST) -- this just collects them, rather than re-declaring a copy here
# that can silently drift from the module's own truth. That drift is exactly
# what used to happen: the old GUI dropdown hardcoded Cluster's address as
# 0x17 (the K-line/KW1281 logical address), when cluster.py's own DEST_CLUSTER
# is 0x07 -- the address the cluster actually answers at over CAN TP2.0
# through the gateway (confirmed by a real VCDS sniff -- see cluster.py). And
# because the GUI always hands Cluster()/Climatronic() an already-opened
# ``tp`` (see _run_cluster/_run_climatronic), their OWN correct ``dest``
# default was silently never consulted -- whatever this table said won
# outright. No user-facing address picker exists at all any more (2026-09-22,
# Bear's call): not for EDC16/ZF6HP, which never needed one, and not for
# Climatronic/Cluster either, since a stale/wrong hardcoded value like the
# 0x17 above is worse than no picker.
_CAN_DEST = {"EDC16": _edc16.CAN_DEST, "Climatronic": _clim.DEST_HVAC,
             "Cluster": _clus.DEST_CLUSTER, "ZF6HP": _zf6hp.DEST,
             "ABS": _abs.DEST_ABS, "Aisin TRx": _aisin09d.DEST}

#: The K-line VDO instrument cluster (VW Mk4 / B5 era: VWK501, VWK503,
#: VMMJ08, VQMJ07, K5MJ07) -- EEPROM read/write over KW1281, see
#: ovtsuite.flash.vdo_cluster. Not the CAN "Cluster" entry (C6 A6, TP 2.0).
VDO_ECU = "Cluster (VDO K-line)"

#: ECU entries that only talk over CAN (TP 2.0, or UDS for the 6HP28), and the
#: ones that only talk over K-line. Choosing one switches Transport to match,
#: and switching Transport away from it switches the ECU to EDC16 (which has
#: both), so the dialog never shows a combination that cannot work.
CAN_ONLY_ECUS = ("Climatronic", "Cluster", "ABS", "ZF6HP")
KLINE_ONLY_ECUS = ("EDC15", VDO_ECU)
#: Units that run on either bus (read-only-style window shape, like the CAN-only
#: ones, but the transport is the user's choice).
EITHER_BUS_TCU = ("Aisin TRx",)
#: Units whose window uses the shared "clim" shape (no EEPROM / backup / boot /
#: staged), whichever bus they are on.
CLIM_SHAPE_ECUS = CAN_ONLY_ECUS + EITHER_BUS_TCU

#: ECU entries that no longer exist, and the entry that replaced each one. A
#: saved setting naming the old entry restores as the new one.
LEGACY_ECUS = {"ZF6HP-UDS": "ZF6HP"}     # 6HP28 merged into ZF6HP (2026-10-11)

#: ZF6HP's flash transport, chosen in Advanced: (value, label). "auto" reads
#: over TP 2.0 and falls back to UDS when nothing answers, and writes over the
#: transport the open file needs (zf6hp_uds.transport_for_session). The values
#: match zf6hp_uds.TP20 / zf6hp_uds.UDS.
ZF6HP_TRANSPORTS = (("auto", "Automatic"),
                    ("tp20", "TP 2.0 (6HP19 / 6HP26)"),
                    ("uds", "UDS (6HP28)"))

EDC16_SPEEDS = ["Very Low (10400)", "Low (38400)", "Medium (124800)", "High (244898)"]
EDC15_SPEEDS = ["Low (10400)", "Medium (38400)", "High (124800)"]
SPEED_KEYS = ["Low", "Medium", "High"]
#: the Aisin TRx on K-line: Low is 10400 with NO speed-up (the original tool's
#: way); Medium / High ask the TCU for 124800 / 244898 and fall back to 10400
AISIN_SPEEDS = ["Low (10400, no speed-up)", "Medium (124800)", "High (244898)"]
EDC16_SPEED_KEYS = ["Very Low"] + SPEED_KEYS
#: the VDO cluster (KW1281, 10400 baud either way): Low is the KWHack dialog
#: (every block ACKed, longer login), High sends the next command as the ACK --
#: see ovtsuite.flash.vdo_cluster
VDO_SPEEDS = ["Low", "High"]
VDO_SPEED_KEYS = ["Low", "High"]


def _as_bool(v, default=True):
    """QSettings may return a bool as a real bool or as a "true"/"false" string."""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _debug_log(msg):
    """Best-effort diagnostic log for the Flash dialog's init/restore chain.

    This app is built --windowed (no console), so an exception raised while a
    Qt signal crosses from C++ back into a Python slot -- e.g. the
    programmatic combo-box changes _restore_settings() makes, which fire
    _transport_changed()/_ecu_changed() via native signal emission rather
    than a plain Python call -- can be silently swallowed by PySide6 with no
    trace anywhere else. This is the only way to see it happened at all.
    Written under the same %APPDATA%/OVT-Suite config dir recent.json uses.
    """
    try:
        from .. import recent as _recent
        path = os.path.join(_recent._config_dir(), "flash_dialog_debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def _port_key(data):
    """A stable, comparable key for a port-combo entry, so a rebuilt list can
    reselect the same device even though J2534 Device objects are recreated
    each scan."""
    if data is None:
        return None
    if data == "openport-usb":
        return ("openport",)
    if data == "sm2-usb":
        return ("sm2",)
    host = getattr(data, "host", None)
    if host is not None:                            # a paired head unit
        return ("remote", host, getattr(data, "port", 0))
    dll = getattr(data, "dll", None)
    if dll is not None:                             # a j2534 Device
        return ("j2534", dll, getattr(data, "bits", 0))
    return ("serial", str(data))


def _device_label(d):
    """Picker text for a J2534 Device: a driver whose cable can't be confirmed on USB
    (no known VID/PID, or a shim/wireless one) stays listed, marked, after the rest."""
    return (d.label + ("" if d.loadable else "  (via 32-bit bridge)")
            + ("  [not detected]" if d.present is None else ""))


def _can_devices():
    """(data, label) CAN interfaces for TP 2.0: J2534 devices (via the 32-bit
    bridge when needed) and a USB OpenPort."""
    out = []
    try:
        from . import j2534 as _j
        for d in _j.plugged_in(_j.list_devices()):
            if _j.usable(d):
                out.append((d, _device_label(d)))
    except Exception:
        pass
    out += openport_devices()
    # A head unit only appears here if the cable at ITS end has a CAN interface --
    # which it told us when we paired, so this is the far cable's answer and not a
    # guess made on this one.
    out += [(u, label) for u, label in remote_devices() if getattr(u, "can", False)]
    return out


def _j2534_devices():
    """(data, label) J2534 devices for K-line: these do ISO 14230 *fast init* in
    hardware (reliable, unlike a bit-banged KKL cable), which the later
    CAN-init-only EDC16 ECUs require."""
    out = []
    try:
        from . import j2534 as _j
        for d in _j.plugged_in(_j.list_devices()):
            if _j.usable(d):
                out.append((d, _device_label(d)))
    except Exception:
        pass
    return out


def list_ports():
    """(device, label) for every usable port: directly-driven FTDI cables
    first (2 ms latency timer, no driver settings to tweak), then the
    OS serial ports."""
    out = []
    try:
        from . import ftdi
        out += ftdi.list_ftdi_ports()
    except Exception:
        pass
    from . import ports as _ports
    for p in _ports.cable_ports():      # real cables only; see ports.py
        label = p.device
        if p.description and p.description != "n/a":
            label = "%s — %s" % (p.device, p.description)
        out.append((p.device, label))
    return out


#: Port-combo data for a Tactrix OpenPort 2.0 driven over USB without the vendor
#: DLL (openport.py) -- the only J2534 route on Linux and macOS.
OPENPORT_USB = "openport-usb"
#: ... and a Scanmatik SM2 over libusb (sm2_device.py), listed with the OpenPorts.
SM2_USB = "sm2-usb"


def is_j2534(data):
    """True if a port-combo entry is a J2534 device (vs a serial-port string):
    a vendor-DLL device, or the USB OpenPort, which presents the same faces."""
    return data in (OPENPORT_USB, SM2_USB) or getattr(data, "dll", None) is not None


def open_j2534(data):
    """Open a J2534 port-combo entry: the USB OpenPort or SM2 directly, a DLL
    device in-process or through the 32-bit bridge."""
    if data == OPENPORT_USB:
        from .openport import OpenPort
        return OpenPort()
    if data == SM2_USB:
        from .sm2_device import Sm2Device
        return Sm2Device()
    from . import j2534 as _j
    return _j.open_device(data)


def openport_devices():
    """(data, label) for each OpenPort 2.0 and each SM2 libusb can open on USB."""
    out = []
    try:
        from . import openport
        out += [(OPENPORT_USB, label + " (Direct-USB)") for label in openport.find_devices()]
    except Exception:
        pass
    try:
        from . import sm2_device
        out += [(SM2_USB, label + " (Direct-USB)") for label in sm2_device.find_devices()]
    except Exception:
        pass
    return out


def no_device_hint() -> str:
    """Why no OpenPort is listed, for an empty or OpenPort-less picker."""
    try:
        from . import openport
        why = openport.explain_absence()
    except Exception:
        why = ""
    try:
        from . import sm2_device
        sm2 = sm2_device.explain_absence()
    except Exception:
        sm2 = ""
    return "; ".join(x for x in (why, sm2) if x)


def is_remote(data):
    """True if a port-combo entry is a paired KW-Dash head unit.

    The cable is in the car and this laptop reaches it over Wi-Fi or Bluetooth.
    To the user it is one more entry in the same picker; to the code it is a link
    like any other, because ``RemoteKLine`` and ``RemoteTp20`` present the same
    faces as the local ones.
    """
    return getattr(data, "host", None) is not None


def remote_devices():
    """(data, label) for every head unit this laptop has paired with.

    Listed without contacting them: a car that is not on the network yet should
    still show up in the picker, so that selecting it is what reports the problem
    rather than its absence being the problem.
    """
    try:
        from . import headunits
        return [(u, u.label) for u in headunits.load()]
    except Exception:
        return []


def kline_devices():
    """(data, label) for the single merged K-line picker: serial/FTDI cables first,
    then J2534 devices (which do ISO 14230 fast init in hardware).  The selected
    entry decides the transport -- a string port -> serial KLine, a J2534 Device ->
    J2534KLine -- so KKL and J2534 K-line are one protocol to the user."""
    out = list(list_ports())
    for d, label in _j2534_devices():
        out.append((d, label + "  (J2534)"))
    out += openport_devices()      # already marked (Direct-USB): no J2534 DLL is involved
    out += remote_devices()
    return out


def cable_allows_high(data):
    """Whether a K-line cable can hold the ~245 kbaud High rate.  Only FTDI-driven
    serial cables can; CH340/CH341 serial cables and J2534 devices are capped at
    Medium (124800), so High is disabled for them (matches diag.cap_high / the
    J2534 baud-grid limit).  That includes an SM2 driven directly over USB: at
    ~245 kbaud its K-line reads come back with random bit errors at every
    requested rate (bench, 2026-11-18), so it stays at Medium too."""
    if data is None or is_j2534(data):
        return False
    if is_remote(data):
        # The far cable's own cap, reported in the greeting. An FTDI lead in the
        # car can hold High; a CH34x or an Openport cannot, and this end has no
        # way to know which without asking.
        return int(getattr(data, "max_speed", 0)) >= 0xA7
    if not isinstance(data, str):
        return False
    try:
        from . import ftdi
        if ftdi.is_ftdi_name(data):
            return True
    except Exception:
        pass
    try:
        from . import diag
        if diag.cable_is_ch34x(data):
            return False
    except Exception:
        pass
    return True                                     # generic serial: allow, user can pick lower


def _session_guard(why):
    """Keep the machine awake (and the process time-critical) for a worker run:
    ``realtime.realtime_session`` when the app has it, else just the sleep guard."""
    try:
        from .. import realtime as _rt
        return _rt.realtime_session(why)
    except Exception:                          # noqa: BLE001 - never block a flash
        return _keepawake.prevent_sleep()


class _Worker(QtCore.QThread):
    stage = QtCore.Signal(str)
    percent = QtCore.Signal(int)
    finished_ok = QtCore.Signal(object)     # image bytes (reads) or None (writes)
    failed = QtCore.Signal(str)
    #: VDO write: the file is the other cluster family's (file type, cluster
    #: type, ident); the dialog asks and answers with answer_vdo_mismatch()
    vdo_mismatch = QtCore.Signal(str, str, str)

    def __init__(self, op, ecu, port, speed, image=None, force_full=False, boot_mode=False,
                 transport="kline", can_dev=None, can_rate=500000,
                 fast_init=True, staged=False, probe=False,
                 hvac_selective=False, edc16=None, uds_cal_only=False,
                 zf_transport="auto", vdo_verify=False):
        super().__init__()
        self._op = op                       # read | write | ee_read | ee_write | backup
        self._ecu = ecu                     # "EDC16" | "EDC15"
        self._boot = boot_mode
        self._port = port                   # serial-port string OR a J2534 Device (K-line)
        self._speed = speed
        self._image = image
        self._force_full = force_full
        self._uds_cal_only = uds_cal_only   # ZF6HP over UDS: write DB_4 only even from a full image
        # ZF6HP: "tp20" | "uds" | "auto". A write always arrives resolved (the
        # dialog picks it from the file or the Advanced override); "auto" only
        # means anything for a read: TP 2.0 first, UDS if nothing answers.
        self._zf_transport = zf_transport
        self._vdo_verify = vdo_verify       # VDO cluster write: read it all back (Advanced)
        self._hvac_selective = hvac_selective   # Climatronic: opt-in selective skip
        self._transport = transport         # "kline" | "can"
        self._fast_init = fast_init         # K-line fast init (break on serial, hw on J2534)
        self._can_dev = can_dev
        self._can_rate = can_rate
        self._can_dest = _CAN_DEST.get(ecu, 0x01)   # fixed per ECU -- see _CAN_DEST above
        self._can = None                    # opened PassThru/OpenPort to close
        self._client = None                 # pass-through connection to close
        self._staged = staged               # the head unit runs the job, we watch
        #: the flash trace of this job (flashtrace.FlashTrace), None while tracing
        #: is off: opened in run(), from Tools > Options > Flasher
        self._trace = None
        self._edc16 = edc16 or {}           # EDC16 block / probe options (gui_edc16.prepare)
        self._probe = probe                 # Cluster/ABS: bus probe instead of a read
        self._stop = threading.Event()
        self._vdo_ask = threading.Event()   # set when the mismatch prompt is answered
        self._vdo_go = False

    def request_stop(self):
        self._stop.set()

    def _vdo_confirm(self, file_type, cluster_type, ident):
        """Worker thread: have the dialog ask, and wait (a Stop answers No)."""
        self._vdo_go = False
        self._vdo_ask.clear()
        self.vdo_mismatch.emit(file_type, cluster_type, ident)
        while not self._vdo_ask.wait(0.2):
            if self._stop.is_set():
                return False
        return self._vdo_go

    def answer_vdo_mismatch(self, go):
        self._vdo_go = bool(go)
        self._vdo_ask.set()

    def _begin_trace(self) -> None:
        """Open this job's flash trace if Tools > Options > Flasher has it on:
        the wire is traced by the links (see _open_link), and the status lines,
        the result and any error are written to it here. Never fails the job."""
        self._trace = None
        try:
            from . import flashtrace
            tr = flashtrace.begin(
                "%s %s (%s)" % (self._ecu, self._op, self._transport),
                log=self.stage.emit)
        except Exception:                   # noqa: BLE001 - tracing is best effort
            return
        if tr is None:
            return
        self._trace = tr
        # Direct: the lines are written (and timed) here, in the job's thread,
        # not queued to the window's
        direct = QtCore.Qt.ConnectionType.DirectConnection
        self._trace_slots = (
            (self.stage, tr.note),
            (self.failed, lambda msg: tr.note("FAILED: %s" % msg)),
            (self.finished_ok, lambda _r: tr.note("finished OK")))
        for sig, slot in self._trace_slots:
            sig.connect(slot, direct)

    def _end_trace(self) -> None:
        tr, self._trace = self._trace, None
        if tr is None:
            return
        for sig, slot in getattr(self, "_trace_slots", ()):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        tr.close()

    def _bridge_trace(self) -> dict:
        """``open_tp20`` / ``open_iso_tp`` arguments that make the 32-bit
        bridge write the CAN frames it carries to the trace (none: no trace)."""
        return self._trace.bridge_trace() if self._trace is not None else {}

    def _open_link(self):
        log = lambda s: self.stage.emit(s)          # noqa: E731
        # the K-line links' byte hook: every byte either way goes to the flash trace
        wire = self._trace.kline_cb() if self._trace is not None else None
        # A head unit is one link either way: the cable is in the car and the
        # timing-critical halves -- both inits, the echo drain, the whole TP 2.0
        # ACK loop -- run at ITS end, which is the only place they can.
        remote = self._can_dev if self._transport == "can" else self._port
        if is_remote(remote):
            from . import headunits
            from .remote import RemoteKLine, RemoteTp20
            self._client = headunits.connect(remote)
            self._client.claim()
            self.stage.emit("Pass-through via %s..." % remote.label)
            if self._transport == "can":
                from .tp20_transport import CanKLine
                return CanKLine(tp=RemoteTp20(self._client, dest=self._can_dest,
                                              rate=self._can_rate, log=log),
                                dest=self._can_dest, log=log)
            return RemoteKLine(self._client, baud=10400,
                               prefer_fast_init=self._fast_init, log=log)
        if self._transport == "can":
            from . import j2534 as _j
            from .tp20_transport import CanKLine
            dev = self._can_dev
            self._can = open_j2534(dev)         # USB OpenPort, in-process or 32-bit bridge
            # Speed fix (2026-09-21): Tp20Params.t3_ms defaults to 5.0ms --
            # deliberately conservative for every OTHER platform on this same
            # code path (its own docstring: advertising 0.1ms once made a
            # Climatronic/HVAC ECU flow-control our requests with 0x9x
            # not-ready). ZF6HP write transfers were coming in ~6x slower
            # than PCMFlash on the same OpenPort 2.0 hardware; comparing our
            # own real write CAN log against the reference capture of a real
            # commercial-tool write (Reference/Logs/6HP-19A CAN Bus
            # Write.log) shows the ONLY meaningfully different negotiated
            # parameter is T3 -- that capture advertises 0.5ms (not 5.0ms)
            # and transfers 199,918 bytes in 33.5s vs. our 134,308 bytes in
            # 142.3s (~6300 B/s vs. ~940 B/s). 0.5ms is real, ZF6HP-specific,
            # hardware-proven evidence (a genuine successful write to this
            # exact TCU family), not a guess -- so it's applied ONLY for
            # ZF6HP, leaving the shared 5.0ms default (and every other
            # platform using it) untouched.
            # Speed fix round 2 (2026-09-21): even after the T3 fix and a
            # precise-wait fix (tp20_transport._precise_wait) closed the gap
            # between intended and actual per-frame timing, real writes still
            # ran ~1.1ms/frame slower than the reference capture. Comparing
            # the reference capture's own A0/A1 negotiation: this SAME 6HP TCU
            # echoes back the same ~0.8ms interframe value it tells us, yet
            # the reference tool still paces at ~0.73ms -- below that floor --
            # with no flow-control issues. That means the reference tool does
            # NOT apply Tp20Channel.send()'s max(ecu_interframe_ms, t3_ms)
            # clamp; it just uses its own advertised t3_ms directly. This is
            # real, ZF6HP-specific, hardware-proven evidence for THIS ECU (not
            # a guess, and not the same risk class as the earlier 0.1ms
            # Climatronic regression, which was a far more aggressive value on
            # a different ECU) -- so ignore_ecu_floor is set for ZF6HP only.
            zf6hp_params = ({"t3_ms": 0.5, "ignore_ecu_floor": True}
                            if self._ecu == "ZF6HP" else {})
            # Climatronic: pace SENDS at the module's OWN advertised inter-frame time
            # (its A1, ~1 ms) instead of the 5 ms max(ecu,t3) clamp -- the clamp made
            # TransferData ~5x slower than the module wants. We still advertise the safe
            # default t3_ms in A0 (pace_ecu only speeds our sends to the ECU's number).
            # pace_ecu: send at the module's advertised ~1 ms interframe.
            # inter_send_ms=4.0: a ~4 ms breather before each chunk (matches ODIS)
            # so the module finishes the previous chunk's flash write before we
            # start the next -- this is what eliminates the 9x not-ready flow
            # control (measured: 1985 -> 9, i.e. ODIS's ~13).
            # tx_ack_every=0: request an ACK only at the ECU's own block size
            # (BS=0x0F=15 from its A1), i.e. ~1 ack per 102-byte chunk. An earlier
            # tx_ack_every=4 was a workaround from BEFORE the inter_send_ms breather
            # existed (acking every 15 with no breather overran the module); with
            # the breather in place the workaround is obsolete and only added
            # ~2.5x the ack round-trips (9393 vs ODIS's 3764), the whole residual
            # gap vs ODIS. Now paced by inter_send_ms, not by forced early acks.
            clim_params = ({"pace_ecu": True, "tx_ack_every": 0, "inter_send_ms": 4.0}
                           if self._ecu == "Climatronic" else {})
            # EDC16: send pacing level and, for a read, the T3 we advertise --
            # see edc16.EDC16_TP20_LEVEL; EDC16_TP20_FAST_TIMING reverts it.
            edc16_params = (_edc16.edc16_tp20_params(self._op)
                            if self._ecu == "EDC16" else {})
            # Through the 32-bit bridge, run the whole TP 2.0 loop IN the bridge
            # (one IPC per KWP message, tight ACK loop in-process) instead of
            # proxying every CAN frame -- avoids the ACK-latency retransmits.
            if isinstance(self._can, _j.BridgePassThru):
                # Flash trace: the bridge's own _FrameTap writes every CAN frame
                # to a file of its own from inside the 32-bit process, so tracing
                # works on the in-bridge TP path too; the trace adds that file to
                # itself when the job ends (flashtrace.FlashTrace.close).
                tp = self._can.open_tp20(dest=self._can_dest, baud=self._can_rate,
                                         params={"block_size": 0x0F, **zf6hp_params,
                                                 **clim_params, **edc16_params},
                                         **self._bridge_trace())
                return CanKLine(tp=tp, dest=self._can_dest, log=log)
            ch = self._can.connect(_j.CAN, self._can_rate)
            ch.pass_all()
            ch.clear_rx()
            if self._trace is not None:
                ch = self._trace.tap(ch)            # every frame, to the trace
            _tp_params = {**zf6hp_params, **clim_params, **edc16_params}
            if _tp_params:
                from .tp20_transport import Tp20Params
                return CanKLine(ch, dest=self._can_dest, log=log,
                                params=Tp20Params(**_tp_params))
            return CanKLine(ch, dest=self._can_dest, log=log)
        # K-line: a J2534 device (hardware fast init) or a serial cable (break pulse)
        if is_j2534(self._port):
            from .kline_j2534 import J2534KLine
            self._can = open_j2534(self._port)  # reused for cleanup
            if self._ecu == "EDC15":
                # EDC15 starts with a KW1281 identification at 9600, whose
                # byte-by-byte complements never form a frame the message link
                # could flush: it needs the byte-stream link, which the KWP2000
                # half of the session then runs on unchanged.
                from .kline_j2534_raw import J2534RawKLine
                self.stage.emit("K-line via %s (5-baud init)..."
                                % getattr(self._port, "name", "J2534"))
                return J2534RawKLine(self._can, baud=9600, log=log, on_trace=wire)
            if self._ecu == "Aisin TRx":
                # The 09D answers a 5-baud init at 0x02 (it takes no fast init) and
                # then talks with gaps between its bytes that the message link's
                # framing loses: on the bench the message link saw only the echo,
                # the byte-stream link (loopback off, K-line only) saw the reply.
                from .kline_j2534_raw import J2534RawKLine
                self.stage.emit("K-line via %s (5-baud init)..."
                                % getattr(self._port, "name", "J2534"))
                return J2534RawKLine(self._can, baud=10400, log=log, on_trace=wire)
            if self._ecu == VDO_ECU:
                # KW1281 end to end (Mk4's custom-block dialog and the Audi C5
                # path alike): the same byte-by-byte complement handshake EDC15's
                # identification stage needs, so the message link's framing is
                # just as useless here. Confirmed on the bench (SM2 and a
                # direct-libusb OpenPort): J2534KLine never got past the
                # cluster's first ident-block byte -- this link read a real
                # cluster's whole EEPROM cleanly on both.
                from .kline_j2534_raw import J2534RawKLine
                self.stage.emit("K-line via %s (5-baud init)..."
                                % getattr(self._port, "name", "J2534"))
                return J2534RawKLine(self._can, baud=10400, log=log, on_trace=wire)
            self.stage.emit("K-line via %s (%s init)..." % (
                getattr(self._port, "name", "J2534"), "fast" if self._fast_init else "5-baud"))
            return J2534KLine(self._can, log=log, fast_init=self._fast_init,
                              on_trace=wire)
        return KLine(self._port, baud=10400, prefer_fast_init=self._fast_init,
                     on_trace=wire)

    def run(self):
        # Sleep/screen-shutoff bug (2026-09-21): a flash read or write can run
        # for several minutes with no mouse/keyboard activity at all, which is
        # exactly what Windows' idle timer watches for -- a real read timed out
        # ~8s after the screen blanked on a 5-minute timer. Hold the system (and
        # display) awake for the whole worker run, released the moment it ends
        # (success, failure, or a user cancel) -- see keepawake.py for why this
        # is a no-op on non-Windows platforms.
        with _session_guard("ECU flash %s" % self._op):
            link = None
            try:
                self._begin_trace()
                if is_remote(self._can_dev if self._transport == "can" else self._port):
                    self.stage.emit("Connecting to the head unit...")
                elif self._transport == "can":
                    self.stage.emit("Opening CAN (TP 2.0)...")
                elif is_j2534(self._port):
                    self.stage.emit("Opening K-line (J2534)...")
                else:
                    self.stage.emit("Opening %s..." % self._port)
                if self._staged:
                    # Nothing is driven from here: the descriptor and the image go over,
                    # and the head unit runs the whole ECU session itself.  That is what
                    # makes a dropped link survivable -- there is no link to drop.
                    self.finished_ok.emit(self._run_staged())
                    return
                if self._ecu == "ZF6HP" and self._zf_transport == "uds":
                    # 6HP28 speaks UDS -- ISO15765 (device ISO-TP) when the device
                    # supports it, else raw CAN. Opened inside the run.
                    if self._op == "read":
                        self._run_zf6hp_read()
                    else:
                        self._run_zf6hp_write()
                    return
                link = self._open_link()
                # Cancel while connecting: the links' init retries (three
                # 5-baud inits per wake-up, a second apart) give up on it.
                # Only those: a read in the middle of a write is not cut.
                try:
                    link.init_is_stop = self._stop.is_set
                except Exception:                   # noqa: BLE001 - a link without it
                    pass
                if self._ecu == "EDC16":
                    self._run_edc16(link)
                elif self._ecu == "Climatronic":
                    self._run_climatronic(link)
                elif self._ecu == "Cluster":
                    self._run_cluster(link)
                elif self._ecu == "ABS":
                    self._run_abs(link)
                elif self._ecu == "ZF6HP":
                    try:
                        self._run_zf6hp(link)
                    except _Tp20NoAnswer as ex:
                        # Automatic read, and nothing answered over TP 2.0: the
                        # TP 2.0 side is finished with (it only ever read), so
                        # close it and read over UDS instead.
                        self._close_quietly(link)
                        link = None
                        self._close_quietly(self._can)
                        self._can = None
                        self.stage.emit("No answer over TP 2.0 (%s). Trying UDS (6HP28)..."
                                        % ex)
                        try:
                            self._run_zf6hp_read()
                        except (_edc16.StopRequested, _edc15.StopRequested,
                                _boot.StopRequested):
                            raise
                        except Exception as ex2:          # noqa: BLE001 - say both
                            if self._stop.is_set():
                                raise
                            raise KLineError(
                                "The TCU answered neither over TP 2.0 (%s) nor over "
                                "UDS (%s)." % (ex, ex2))
                elif self._ecu == "Aisin TRx":
                    self._run_aisin09d(link)
                elif self._ecu == VDO_ECU:
                    self._run_vdo(link)
                else:
                    self._run_edc15(link)
            except (_edc16.StopRequested, _edc15.StopRequested, _boot.StopRequested):
                self.failed.emit("Stopped.")
            except (KLineError, Exception) as ex:      # noqa: BLE001 - surfaced to UI
                # a link that gave up on Cancel (LinkStopped, "stop requested")
                self.failed.emit("Stopped." if self._stop.is_set() else str(ex))
            finally:
                if link is not None:
                    try:
                        link.close()
                    except Exception:
                        pass
                if self._can is not None:
                    try:
                        self._can.close()
                    except Exception:
                        pass
                try:
                    if self._client is not None:
                        try:
                            # Give the cable back so the dashboard starts polling again.
                            # The socket dropping would do it too, but only once the head
                            # unit noticed, and a driver watching dead gauges should not
                            # have to wait for a TCP timeout.
                            self._client.release()
                        finally:
                            self._client.close()
                            self._client = None
                finally:
                    self._end_trace()

    @staticmethod
    def _close_quietly(obj):
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass

    #: How long the ECU is left alone between the two halves of a backup or a restore.
    #: The reference's number: it is rebooting out of one loader and into the next.
    REBOOT_SECONDS = 10

    def _run_staged_pair(self):
        """A backup or a restore, as two staged jobs with a reboot between them."""
        import time

        from . import staged as _staged

        restoring = self._op == "restore"

        if restoring:
            flash_image, eeprom_image = self._image
            first, first_blob = _staged.build_edc15_write_job(
                flash_image, job_id="restore-flash", speed=self._speed,
                force_full=self._force_full)
            second, second_blob = _staged.build_edc15_eeprom_write_job(
                eeprom_image, job_id="restore-eeprom")
        else:
            first, first_blob = _staged.build_edc15_read_job(
                job_id="backup-flash", speed=self._speed)
            second, second_blob = _staged.build_edc15_eeprom_read_job(
                job_id="backup-eeprom")

        self.stage.emit("Staging %s..." % _staged.describe(first))
        self._client.stage(first, first_blob)
        self._client.start()
        self._watch(lambda p: self.percent.emit(p * 90 // 100))
        flash_result = None if restoring else self._client.fetch()

        # The wait is HERE and not in the descriptor, because nothing is talking to the
        # ECU during it. Interruptible, because ten seconds is long enough to change
        # your mind and the head unit is not holding anything open.
        for i in range(self.REBOOT_SECONDS * 5):
            if self._stop.is_set():
                self._stop.clear()
                raise _edc16.StopRequested("stopped")
            self.stage.emit(
                "%s. Waiting %d s for the ECU to reboot..."
                % ("Flash written" if restoring else "Flash read",
                   self.REBOOT_SECONDS - i // 5))
            time.sleep(0.2)

        self.stage.emit("Staging %s..." % _staged.describe(second))
        self._client.stage(second, second_blob)
        self._client.start()
        self._watch(lambda p: self.percent.emit(90 + p * 10 // 100))

        if restoring:
            self.stage.emit("Flash and EEPROM both written.")
            return None

        return (flash_result, self._client.fetch())

    def _watch(self, on_percent):
        """Poll a running job to its end. Raises whatever the head unit reported."""
        import time

        while True:
            if self._stop.is_set():
                # A request, never a kill -- the head unit honours it at its own safe
                # points, because stopping between an erase and its transfer is a
                # recovery job rather than a cancelled one.
                self._client.cancel()
                self._stop.clear()

            reply = self._client.status()
            total = max(1, int(reply.get("total", 1)))
            on_percent(min(100, int(reply.get("done", 0)) * 100 // total))
            if reply.get("stage"):
                self.stage.emit(reply["stage"])

            status = reply.get("status")
            if status == "finished":
                return reply
            if status == "failed":
                raise KLineError(reply.get("error", "the head unit reported a failure"))
            if status == "cancelled":
                raise _edc16.StopRequested("stopped")

            time.sleep(0.5)

    def _run_staged(self):
        """Stage the job, start it, and watch. Returns the image for a read, else None."""
        from . import headunits
        from . import staged as _staged

        unit = self._can_dev if self._transport == "can" else self._port
        transport = "can" if self._transport == "can" else "kline"

        if self._op not in ("read", "write", "ee_read", "ee_write",
                            "backup", "restore"):
            # Belt and braces with `_staged_available`, which greys the box out for these.
            # Reaching here would mean building a flash job from an EEPROM image, and the
            # message that came back would be about image length rather than about this.
            raise KLineError(
                "The head unit has no staged job for %r; untick 'Run on the head unit'."
                % self._op)

        self.stage.emit("Connecting to %s..." % getattr(unit, "name", "the head unit"))
        self._client = headunits.connect(unit)
        self._client.claim()

        if self._op in ("backup", "restore"):
            # TWO JOBS, not a third kind of job. A backup is a flash read and an EEPROM
            # read; a restore is a sector write and an EEPROM write. Each half is already
            # its own session with its own loader, and what separates them is the
            # controller rebooting -- ten seconds in which there is no conversation at
            # all. So the composition has no timing in it and belongs here, on the
            # laptop, where a staged executor for it would only be a worse copy of the
            # two that already exist.
            return self._run_staged_pair()

        if self._boot:
            self.stage.emit("Preparing boot mode...")
            if self._op == "read":
                job, blob = _staged.build_edc15_boot_read_job()
            else:
                job, blob = _staged.build_edc15_boot_write_job(self._image)
        elif self._op == "ee_read":
            self.stage.emit("Preparing the EEPROM loader...")
            job, blob = _staged.build_edc15_eeprom_read_job()
        elif self._op == "ee_write":
            self.stage.emit("Preparing the EEPROM loader...")
            job, blob = _staged.build_edc15_eeprom_write_job(self._image)
        elif self._ecu == "EDC15" and self._op == "read":
            self.stage.emit("Preparing the loader...")
            # Variant V, like the other eight EDC15 call sites in this dialog: it is the
            # only one the tuner drives, and staged must not be the path that quietly
            # differs.
            job, blob = _staged.build_edc15_read_job(job_id="read", speed=self._speed)
        elif self._ecu == "EDC15":
            # No cipher and no blocks: an EDC15 write ships the loader and the whole
            # image, and the head unit asks the controller which sectors differ.
            self.stage.emit("Preparing sectors...")
            job, blob = _staged.build_edc15_write_job(
                self._image, job_id="write", speed=self._speed,
                force_full=self._force_full)
        elif self._op == "read":
            job = _staged.build_read_job(job_id="read", transport=transport,
                                         speed=self._speed)
            blob = b""
        else:
            self.stage.emit("Encrypting...")
            job, blob = _staged.build_write_job(self._image, job_id="write",
                                                transport=transport, speed=self._speed)

        # The last moment at which refusing is free: everything checkable without
        # touching the ECU is checked here, and after this there may be an erased block.
        self.stage.emit("Staging %s..." % _staged.describe(job))
        self._client.stage(job, blob)
        self._client.start()

        # The same watcher a backup or a restore uses for each of its halves. A second
        # copy of this loop is a second place for "cancelled" to be mistaken for
        # "failed", and the two mean very different things to whoever is holding the car.
        reply = self._watch(self.percent.emit)

        if self._op not in ("read", "ee_read", "ee_write"):
            # A total of zero means the head unit found nothing to write -- every sector
            # already matched the image. Saying "write complete" there would be true only
            # in the sense that nothing remained to do, which is not how anyone reads it.
            if int(reply.get("total", 1)) == 0:
                self.stage.emit("Nothing to write: the controller already holds this image.")
                return None

            # Whatever the controller needs afterwards - an EDC16 wants the ignition
            # off for five seconds - travels with the job rather than being known here.
            self.stage.emit(job.get("completion", "Write complete."))
            return None

        if self._op == "ee_write":
            # A write that also hands something back: the head unit returns what it READ
            # after writing, and that -- not the image that was sent -- is what the
            # driver is shown decoded afterwards.
            self.stage.emit(
                "The EEPROM already matched the image."
                if int(reply.get("total", 1)) == 0
                else job.get("completion", "EEPROM written and verified."))
            return self._client.fetch()

        self.stage.emit("Collecting the image...")
        collected = self._client.fetch()

        # An EEPROM and an EDC15 flash read both hand back exactly what was asked for;
        # an EDC16 hands back the regions, and padding those into a 2 MB image is a
        # statement about ONE controller's memory map that belongs at this end.
        if self._boot or self._op == "ee_read" or self._ecu == "EDC15":
            return collected
        return _staged.assemble_read(collected)

    def _run_edc16(self, link):
        flash = _edc16.Edc16Flash(link, log=lambda s: self.stage.emit(s))
        # CAN/TP 2.0 has no K-line baud step: run "fast" (batched) with no
        # elevated-baud handshake.
        upload_chunk = None
        if self._transport == "can":
            flash._fast = True
            allow_fast = False
            upload_chunk = 0x10000          # CAN: read as 64 KB uploads (like MPPS)
        else:
            allow_fast = True
        fast_init_prime = "auto" if self._fast_init else False
        stage = self.stage.emit            # program/probe ops log straight to the UI
        if self._op == "read":
            img = flash.read_flash(
                on_percent=self.percent.emit, on_stage=stage,
                is_stop=self._stop.is_set, speed=self._speed, allow_fast=allow_fast,
                upload_chunk=upload_chunk)
            self.finished_ok.emit(img)
        elif self._op == "write":
            flash.write_flash(
                self._image, on_percent=self.percent.emit, on_stage=stage,
                is_stop=self._stop.is_set, force_full=self._force_full,
                speed=self._speed, allow_fast=allow_fast,
                fast_init_prime=fast_init_prime)
            self.finished_ok.emit(None)
        elif self._op in _gui16.BLOCK_OPS or self._op in _gui16.RESEARCH_OPS:
            self.finished_ok.emit(_gui16.run(self, flash, allow_fast, fast_init_prime))
        else:
            raise KLineError("EEPROM access is only available for EDC15.")

    def _probe_bus(self, link, ecu):
        """Diagnostic (read-only): remove the id filter, send ONE TP2.0 channel-setup
        broadcast to 0x200 for self._can_dest at the already-open CAN rate, and log
        every frame for ~2s. In-process / OpenPort path only (the 32-bit bridge runs
        its own TP loop and doesn't expose the raw channel). Emits a summary dict."""
        tp = getattr(link, "tp", None)
        chan = getattr(tp, "can", None) if tp is not None else None
        if chan is None:
            raise KLineError(
                "Bus probe needs the in-process CAN path (OpenPort / J2534 direct), "
                "not the 32-bit bridge.")
        # (every frame it sends or hears goes to the flash trace when that is on:
        # the channel is the tapped one -- see _open_link)
        self.stage.emit("Bus probe: dest 0x%02X at %d bps" % (self._can_dest,
                                                               self._can_rate))
        summary = _clim.probe_channel(
            chan, dest=self._can_dest, seconds=2.0,
            emit=lambda s: self.stage.emit(s))
        summary["ecu"] = ecu
        summary["baud"] = self._can_rate
        self.finished_ok.emit(summary)

    def _run_climatronic(self, link):
        """Climatronic (4F/4L 910 043x) over TP 2.0 -- reuses the already-open
        TP channel behind the CanKLine (link.tp). WRITE only: the module has no
        OBD read."""
        tp = getattr(link, "tp", None)
        if tp is None:
            raise KLineError("Climatronic needs the CAN / TP 2.0 transport.")
        if self._op != "write":
            raise KLineError("The Climatronic can't be read over OBD; it can only "
                             "be written.")
        try:
            tp.is_stop = self._stop.is_set              # let Cancel abort a stuck wait
        except Exception:
            pass
        hvac = _clim.Climatronic(tp=tp, log=lambda s: self.stage.emit(s))
        hvac.open()
        # Programming-session flash driven from an .sgo/.hvac. flash_write() does
        # the programming session (10 85) + write security (27 01/02, key
        # seed+0x05814A05) itself and handles the ECU-initiated channel reconnect
        # after each erase -- the exact car-captured ODIS sequence.
        src = self._image
        from .hvac_image import HvacImage, HvacImageError
        if isinstance(src, HvacImage):
            # The GUI hands over the image as open in the app (unsaved
            # edits included, checksum already handled there).
            himg, src = src, (src.name or "the open image")
        elif isinstance(src, str):
            try:
                himg = HvacImage.load(src)
            except HvacImageError as ex:
                raise KLineError(str(ex))
        else:
            raise KLineError("Climatronic write needs a loaded .sgo/.hvac file.")
        if himg.loader is None:
            raise KLineError(
                "This image carries no flash loader -- flash from the original "
                ".sgo/.hvac (which includes it), not a raw .bin.")
        blocks = himg.to_blocks(fix_checksum=False)
        self.stage.emit("Flash source: %s -- %d blocks" % (src, len(blocks)))
        # Selective skip (opt-in): only sections that differ from the stock
        # baseline are written. changed_section_addrs() returns a set for a
        # .hvac (which stores the baseline) or None for a .sgo (-> flash all).
        changed = himg.changed_section_addrs() if self._hvac_selective else None
        if self._hvac_selective:
            if changed is None:
                self.stage.emit("Skip-unchanged: no baseline in this file "
                                "(.sgo) -- writing all sectors.")
            else:
                self.stage.emit("Skip-unchanged: %d of %d sections changed "
                                "vs baseline; the rest will be skipped."
                                % (len(changed), len(blocks)))
        # Status is driven CONTINUOUSLY from on_percent (per 0x36 chunk)
        # so the label always reflects writing progress and can never sit
        # on a stale "channel reconnected" while a section streams. on_block
        # records which block; on_percent emits "Writing block N/M - P%" and
        # only when the integer % changes (avoids flooding the label).
        _wr = {"blk": "", "pct": -1}
        def _on_block(i, n, a, sz, _wr=_wr):
            _wr["blk"] = "block %d/%d @ 0x%06X" % (i + 1, n, a)
        def _on_pct(done, tot, _wr=_wr):
            pct = int(done * 100 / max(1, tot))
            self.percent.emit(pct)
            if pct != _wr["pct"]:
                _wr["pct"] = pct
                self.stage.emit("Writing %s - %d%%" % (_wr["blk"], pct))
        hvac.flash_write(
            blocks, changed=changed, is_stop=self._stop.is_set,
            on_block=_on_block, on_percent=_on_pct)
        self.percent.emit(100)
        self.finished_ok.emit(None)

    def _run_cluster(self, link):
        """C6 instrument cluster (4F0910930x) over TP 2.0 -- reuses the open TP
        channel behind the CanKLine (link.tp).  READ + identify only.

        The cluster's security seed/key is not yet known, so the unlock is
        attempted but a failure is NON-fatal: the read is tried anyway (some
        clusters allow ReadMemoryByAddress without 0x27, and any 27-exchange is
        captured when the flash trace is on (Tools > Options > Flasher) -- which
        is how we would learn the algorithm)."""
        if self._probe:
            return self._probe_bus(link, "Cluster")
        tp = getattr(link, "tp", None)
        if tp is None:
            raise KLineError("Cluster needs the CAN / TP 2.0 transport.")
        try:
            tp.is_stop = self._stop.is_set              # let Cancel abort a stuck wait
        except Exception:
            pass
        clu = _clus.Cluster(tp=tp, log=lambda s: self.stage.emit(s))
        clu.open()
        try:
            clu.start_session(0x89)
        except (_clus.ClusterError, KLineError) as ex:
            self.stage.emit("(session 0x89: %s -- continuing)" % ex)
        try:
            rec = clu.read_ecu_identification()
            self.stage.emit("ECU ID: %s" % rec.hex())
        except (_clus.ClusterError, KLineError) as ex:
            self.stage.emit("(ECU ID read: %s -- continuing)" % ex)
        try:
            clu.unlock()                                # seed/key unknown -> non-fatal
        except (_clus.ClusterError, KLineError) as ex:
            self.stage.emit("(security access: %s -- trying read without unlock)" % ex)
        if self._op == "read":
            img = clu.dump(on_percent=self.percent.emit, is_stop=self._stop.is_set)
            self.finished_ok.emit(img)
        elif self._op == "write":
            raise KLineError("Cluster write is not implemented yet (read-only).")
        else:
            raise KLineError("Only flash read is available for the cluster.")

    def _run_abs(self, link):
        """C6 Bosch ESP8 ABS (4F0910517xx) over TP 2.0 -- reuses the open TP
        channel behind the CanKLine (link.tp). READ + identify only in this
        version. Both SecurityAccess levels are known (write/programming
        27 01/02 = the SGO SA2 tape; read/diagnostic 27 03/04 = seed+0x2BD7);
        the full-image dump uses the programming level + 10 85, driven inside
        AbsEsp8.dump(). OBD write needs a RAM flash-loader from an ODIS capture
        and is not implemented yet."""
        if self._probe:
            return self._probe_bus(link, "ABS")
        tp = getattr(link, "tp", None)
        if tp is None:
            raise KLineError("ABS needs the CAN / TP 2.0 transport.")
        try:
            tp.is_stop = self._stop.is_set
        except Exception:
            pass
        ecu = _abs.AbsEsp8(tp=tp, log=lambda s: self.stage.emit(s))
        ecu.open()
        try:
            ecu.start_session(0x89)
        except (_abs.AbsError, KLineError) as ex:
            self.stage.emit("(session 0x89: %s -- continuing)" % ex)
        try:
            rec = ecu.read_ecu_identification()
            self.stage.emit("ECU ID: %s" % rec.hex())
        except (_abs.AbsError, KLineError) as ex:
            self.stage.emit("(ECU ID read: %s -- continuing)" % ex)
        if self._op == "read":
            img = ecu.dump(on_percent=self.percent.emit, is_stop=self._stop.is_set)
            self.finished_ok.emit(img)
        elif self._op == "write":
            raise KLineError("ABS write is not implemented yet (read-only): the OBD "
                             "flash-write framing is not yet confirmed for this module.")
        else:
            raise KLineError("Only flash read is available for the ABS.")

    def _run_zf6hp(self, link):
        """ZF 6HP TCU (4F9910156A-family) over TP 2.0 -- reuses the already-open
        TP channel behind the CanKLine (link.tp). Both read and write are
        supported: the read-level and write-level security keys, and the
        transferData (write payload) cipher, are all solved (see the module
        docstring in ovtsuite.flash.zf6hp). Zf6hpFlash.read_image() /
        write_image() are both fully self-contained -- session start, the
        security unlock, and the whole read/write loop all happen inside
        them -- so there's no separate open/session/unlock dance to drive
        here."""
        if self._probe:
            return self._probe_bus(link, "ZF6HP")
        tp = getattr(link, "tp", None)
        if tp is None:
            raise KLineError("ZF6HP needs the CAN / TP 2.0 transport.")
        try:
            tp.is_stop = self._stop.is_set              # let Cancel abort a stuck wait
        except Exception:
            pass
        flash = _zf6hp.Zf6hpFlash(tp=tp, log=lambda s: self.stage.emit(s))
        if self._op == "read":
            self.stage.emit("Reading over TP 2.0 (6HP19 / 6HP26)...")
            try:
                img = flash.read_image(on_percent=self.percent.emit,
                                       is_stop=self._stop.is_set)
            except Exception as ex:
                # Automatic only: fall back to UDS when TP 2.0 never got as
                # far as one KWP answer -- the channel setup failed, or the
                # TCU stayed silent. Any answer (even a refusal) means this
                # IS a TP 2.0 TCU and the error stands; so does a cancel.
                if (self._zf_transport == "auto" and not flash.answered
                        and not self._stop.is_set() and _is_link_failure(ex)):
                    raise _Tp20NoAnswer(str(ex)) from ex
                raise
            self.finished_ok.emit(img)
        elif self._op == "write":
            # No on_stage= here: the constructor's log= callback above already
            # routes write_areas' internal stage() calls to self.stage.emit,
            # so passing on_stage too would double-emit every stage message.
            # force_full=self._force_full bypasses the per-area checksum
            # pre-check (see zf6hp.Zf6hpFlash.write_areas' docstring) and
            # rewrites both areas unconditionally, same as the EDC16/EDC15
            # "Force full write" checkbox already does for their own
            # changed-sector/block checks.
            flash.write_image(self._image, on_percent=self.percent.emit,
                              is_stop=self._stop.is_set, force_full=self._force_full)
            self.finished_ok.emit(None)
        else:
            raise KLineError("Only flash read/write are available for the ZF6HP.")

    def _run_aisin09d(self, link):
        """Aisin TRx (VW 09D TR-60SN) TCU over TP 2.0 -- reuses the already-open
        TP channel behind the CanKLine (link.tp), same as ZF6HP. READ is the
        full 1 MB flash dump via KWP2000 readMemoryByAddress (no security
        needed); Aisin09dFlash.read_image() drives the whole session/read loop.
        WRITE is implemented in Aisin09dFlash.write_image (plaintext
        transferData, the solved 09D checksum) but only proven against the
        simulator, so the UI keeps this module read-only until it is confirmed
        on a real TCU."""
        log = lambda s: self.stage.emit(s)
        if self._transport == "can":
            tp = getattr(link, "tp", None)
            if tp is None:
                raise KLineError("Aisin TRx needs the CAN / TP 2.0 transport.")
            try:
                tp.is_stop = self._stop.is_set          # let Cancel abort a stuck wait
            except Exception:
                pass
            flash = _aisin09d.Aisin09dFlash(tp=tp, log=log)
            size = _aisin09d.IMAGE_SIZE
        else:
            # K-line: the same KWP2000 requests, framed ISO 14230 on the cable.
            # Fast init first (5-baud as the fallback), then the same baud
            # escalation as EDC16, back to 10400 if the TCU won't follow. The
            # size is automatic: 1 MiB, or 512 KiB where the flash ends there.
            speed = self._speed
            if speed == "High" and not cable_allows_high(self._port):
                speed = "Medium"
            flash = _aisin09d.Aisin09dFlash(kline=link, log=log, speed=speed)
            flash.tp.is_stop = self._stop.is_set
            size = _aisin09d.KLINE_IMAGE_SIZE
        if self._op == "read":
            img = flash.read_image(on_percent=self.percent.emit, is_stop=self._stop.is_set,
                                   image_size=size)
            self.finished_ok.emit(img)
        elif self._op == "write":
            # write_image() itself no longer refuses (it is implemented, but
            # proven only against trx_sim), so the refusal lives here as well as
            # in the disabled Write button -- see aisin09d's module docstring.
            raise KLineError("Aisin TRx write is not enabled in the flasher yet: it "
                             "has not been confirmed on a real TCU (read-only).")
        else:
            raise KLineError("Only flash read is available for the Aisin TRx.")

    def _run_vdo(self, link):
        """VDO instrument cluster EEPROM over K-line KW1281 (port of KWHack's
        cluster path, see ovtsuite.flash.vdo_cluster). Read hands back the
        EEPROM plus what the cluster reported about itself; write writes the
        whole EEPROM and reads it back (nothing is read first)."""
        if self._transport == "can":
            raise KLineError("The VDO cluster is K-line only (KW1281 at 0x17).")
        log = lambda m: self.stage.emit(m)
        if self._op == "read":
            data, meta = _vdo.read_cluster(link, log=log, on_percent=self.percent.emit,
                                           is_stop=self._stop.is_set,
                                           fast=self._speed == "High")
            self.finished_ok.emit({"_vdo": "read", "eeprom": data, "meta": meta})
            return
        if self._op != "write":
            raise KLineError("Only read and write are available for the cluster.")
        rep = _vdo.write_cluster(link, self._image, log=log,
                                 on_percent=self.percent.emit,
                                 is_stop=self._stop.is_set,
                                 fast=self._speed == "High",
                                 verify=self._vdo_verify,
                                 confirm_mismatch=self._vdo_confirm)
        self.finished_ok.emit({"_vdo": "write", "report": rep})

    def _tap_6hp_can(self, ch, tag):
        """Wrap a raw CAN channel in a frame tap that keeps a TX/RX tally
        (TX-but-no-RX is the decisive datum when a read "gets no response") and,
        when the flash trace is on, writes every frame to it. The tally is
        always folded into the status / error message. Returns
        (wrapped_channel, bus) where ``bus`` has ``.summary()`` and ``.close()``."""
        from . import zf6hp_uds as _uds
        from .flashtrace import TracedChannel
        wrapped = TracedChannel(ch, self._trace)
        if self._trace is not None:
            self._trace.note("6HP28 UDS %s: tx=0x%03X rx=0x%03X rate=%s"
                             % (tag, _uds.REQ_ID, _uds.RESP_ID, self._can_rate))
        trace = self._trace

        class _Bus:
            def summary(_s):
                where = ("; trace: %s" % trace.path) if trace is not None else ""
                return " [bus: TX=%d RX=%d%s]" % (wrapped.tx, wrapped.rx, where)

            def close(_s):
                pass

        return wrapped, _Bus()

    def _run_zf6hp_read(self):
        """Read the 6HP28 calibration over UDS (read-only). Prefers ISO15765 --
        the device/firmware does ISO-TP (one write + one read per chunk, like the
        OEM tool, and no per-frame bridge IPC) -- but opens the gateway with a
        raw-CAN functional (0x7DF) wake FIRST, because an ISO15765 channel can't
        frame the wake broadcast (no flow-control filter for 0x7DF), so its own
        wake never opens routing (2026-10-08 car trace: ISO15765 wake, zero
        0x7E9, connect failed). Falls back to raw CAN + software ISO-TP (the
        proven path) if the device rejects ISO15765 OR if an ISO15765 session
        can't be established. Set OVT_6HP_FORCE_RAW=1 to force the raw path (for
        A/B testing on the car). Either way Zf6hpUdsReader does session 0x4F +
        read security + readMemoryByAddress over the calibration window."""
        import os as _os
        from . import zf6hp_uds as _uds
        from . import j2534 as _j
        dev = self._can_dev
        force_raw = _os.environ.get("OVT_6HP_FORCE_RAW", "").strip() not in ("", "0", "false", "False", "no")
        self.stage.emit("Opening CAN (UDS)...")
        self._can = open_j2534(dev)
        self.stage.emit("CAN device: %s" % (getattr(dev, "name", None) or dev))
        if force_raw:
            self.stage.emit("OVT_6HP_FORCE_RAW set -- using the per-frame raw CAN path.")

        # 32-bit bridge (e.g. VXDIAG): run the ISO-TP engine IN-BRIDGE so each
        # UDS chunk is ONE IPC (reassembly + responsePending next to the DLL)
        # instead of ~35 per-frame read round-trips -- the cause of the slow,
        # stalling read over the bridge. Such clone DLLs also commonly fail to
        # deliver ISO15765 receives (2026-10-09 car trace: TX ok, zero 0x7E9),
        # so the in-bridge raw drain is the fast AND reliable path there.
        # OVT_6HP_FORCE_RAW=1 forces the old per-frame path for A/B comparison.
        if isinstance(self._can, _j.BridgePassThru) and not force_raw:
            bt = self._bridge_trace()          # the flash trace, if on (the bridge's half)
            try:
                io = self._can.open_iso_tp(
                    self._can_rate, tx_id=_uds.REQ_ID, rx_id=_uds.RESP_ID,
                    func_id=_uds.FUNC_ID, **bt,
                    log=lambda m: self.stage.emit(m), is_stop=self._stop.is_set)
            except Exception as ex:             # old bridge build without iso_* ops
                self.stage.emit("In-bridge ISO-TP unavailable (%s); using the "
                                "per-frame path." % ex)
            else:
                self.stage.emit("Read transport: in-bridge ISO-TP (one IPC per chunk)."
                                + (" CAN frames go to the trace when it ends." if bt else ""))
                reader = _uds.Zf6hpUdsReader(
                    io, on_stage=lambda m: self.stage.emit(m),
                    on_percent=self.percent.emit, is_stop=self._stop.is_set)
                try:
                    cal = reader.read_calibration()
                finally:
                    try:
                        io.close()
                    except Exception:
                        pass
                self.stage.emit("Read complete (in-bridge ISO-TP).")
                self.finished_ok.emit({"_uds_read": True, "cal": cal,
                                       "base": _uds.CAL_READ_BASE})
                return

        def _open(prefer_iso):
            ch, kind, awake = _uds.open_6hp_transport(
                self._can, self._can_rate, prefer_iso15765=prefer_iso,
                log=lambda m: self.stage.emit(m), is_stop=self._stop.is_set)
            tapped, bus = self._tap_6hp_can(ch, "read" if prefer_iso else "read-raw")
            if kind == "iso15765":
                io = _uds.Iso15765Io(tapped, log=lambda m: self.stage.emit(m),
                                     is_stop=self._stop.is_set)
                self.stage.emit("Read transport: ISO15765 (device handles ISO-TP).")
            else:
                io = _uds.IsoTp(tapped, log=lambda m: self.stage.emit(m),
                                is_stop=self._stop.is_set)
                self.stage.emit("Read transport: raw CAN (software ISO-TP).")
            reader = _uds.Zf6hpUdsReader(
                io, on_stage=lambda m: self.stage.emit(m),
                on_percent=self.percent.emit, is_stop=self._stop.is_set,
                assume_awake=(kind == "iso15765" and awake))
            return reader, kind, bus

        reader, kind, _bus = _open(prefer_iso=not force_raw)
        try:
            cal = reader.read_calibration()
        except Exception as ex:                 # noqa: BLE001 - re-raised with bus tally
            _bus.close()
            if kind == "iso15765" and not self._stop.is_set():
                # The device took ISO15765 but no session came up; drop to the
                # proven raw-CAN path (its own wake re-opens gateway routing).
                self.stage.emit("ISO15765 did not establish a session (%s); "
                                "falling back to raw CAN + software ISO-TP." % ex)
                reader, kind, _bus = _open(prefer_iso=False)
                try:
                    cal = reader.read_calibration()
                except Exception as ex2:        # noqa: BLE001
                    _bus.close()
                    raise KLineError(str(ex2) + _bus.summary())
            else:
                raise KLineError(str(ex) + _bus.summary())
        _bus.close()
        self.stage.emit("Read complete." + _bus.summary())
        self.finished_ok.emit({"_uds_read": True, "cal": cal, "base": _uds.CAL_READ_BASE})

    def _run_zf6hp_write(self):
        """Write a 6HP28 image over UDS. Prefers ISO15765 (the device does ISO-TP,
        like the OEM tool -- one write+read per step, firmware handles flow
        control/retransmit) and falls back to raw CAN + software ISO-TP if the
        device rejects it. Runs Zf6hpUdsWriter on the loaded flash image."""
        import os as _os
        from . import zf6hp_uds as _uds
        from . import j2534 as _j
        dev = self._can_dev
        force_raw = _os.environ.get("OVT_6HP_FORCE_RAW", "").strip() not in ("", "0", "false", "False", "no")
        self.stage.emit("Opening CAN (UDS)...")
        self._can = open_j2534(dev)
        self.stage.emit("CAN device: %s" % (getattr(dev, "name", None) or dev))
        image = self._image
        if isinstance(image, str):
            with open(image, "rb") as fh:
                image = fh.read()

        # 32-bit bridge (e.g. VXDIAG): run the ISO-TP engine IN-BRIDGE so the
        # block transfer streams next to the DLL (one IPC per request instead of
        # per-frame round-trips) -- the same move that took the 6HP19 write to
        # ~4x. The writer does not wake the gateway itself, so open routing with
        # a functional (0x7DF) wake first. OVT_6HP_FORCE_RAW=1 forces the old
        # per-frame path for A/B.
        if isinstance(self._can, _j.BridgePassThru) and not force_raw:
            bt = self._bridge_trace()          # the flash trace, if on (the bridge's half)
            try:
                io = self._can.open_iso_tp(
                    self._can_rate, tx_id=_uds.REQ_ID, rx_id=_uds.RESP_ID,
                    func_id=_uds.FUNC_ID, **bt,
                    log=lambda m: self.stage.emit(m), is_stop=self._stop.is_set)
            except Exception as ex:             # old bridge build without iso_* ops
                self.stage.emit("In-bridge ISO-TP unavailable (%s); using the "
                                "per-frame path." % ex)
            else:
                self.stage.emit("Write transport: in-bridge ISO-TP (one IPC per request)."
                                + (" CAN frames go to the trace when it ends." if bt else ""))
                _uds.wake_gateway(io, log=lambda m: self.stage.emit(m))
                writer = _uds.Zf6hpUdsWriter(
                    io, on_stage=lambda m: self.stage.emit(m),
                    on_percent=self.percent.emit, is_stop=self._stop.is_set)
                try:
                    plan = writer.write(bytes(image), force_full=self._force_full,
                                        cal_only=self._uds_cal_only)
                finally:
                    try:
                        io.close()
                    except Exception:
                        pass
                self.stage.emit("Write complete (in-bridge ISO-TP).")
                self.finished_ok.emit({"_uds_write": True, "report": plan.report(),
                                       "written": [b.spec.name for b in plan.write_set]})
                return

        use_15765 = False
        try:
            _io15765, ch = _uds.connect_iso15765(self._can, self._can_rate,
                                                 log=lambda m: self.stage.emit(m))
            use_15765 = True
        except Exception as ex:                 # noqa: BLE001 - any failure -> raw-CAN fallback
            self.stage.emit("ISO15765 unavailable (%s); using raw CAN + software ISO-TP."
                            % ex)
            ch = self._can.connect(_j.CAN, self._can_rate)
            try:
                ch.pass_all()
                ch.clear_rx()
            except Exception:
                pass
        ch, _bus = self._tap_6hp_can(ch, self._op)
        if use_15765:
            io = _uds.Iso15765Io(ch, log=lambda m: self.stage.emit(m),
                                 is_stop=self._stop.is_set)
            self.stage.emit("Write transport: ISO15765 (device handles ISO-TP).")
        else:
            io = _uds.IsoTp(ch, log=lambda m: self.stage.emit(m), is_stop=self._stop.is_set)
            self.stage.emit("Write transport: raw CAN (software ISO-TP).")
        writer = _uds.Zf6hpUdsWriter(io, on_stage=lambda m: self.stage.emit(m),
                                     on_percent=self.percent.emit, is_stop=self._stop.is_set)
        try:
            plan = writer.write(bytes(image), force_full=self._force_full,
                                cal_only=self._uds_cal_only)
            self.finished_ok.emit({"_uds_write": True, "report": plan.report() + _bus.summary(),
                                   "written": [b.spec.name for b in plan.write_set]})
        except Exception as ex:                 # noqa: BLE001 - re-raised with bus tally
            raise KLineError(str(ex) + _bus.summary())
        finally:
            _bus.close()

    def _run_edc15(self, link):
        stop = self._stop.is_set
        if self._boot and self._op in ("read", "write"):
            bm = _boot.Edc15BootMode(link, log=lambda s: self.stage.emit(s))
            if self._op == "read":
                bm.connect_read(is_stop=stop, on_stage=self.stage.emit)
                img = bm.read_flash(on_percent=self.percent.emit,
                                    on_stage=self.stage.emit, is_stop=stop)
                self.finished_ok.emit(img)
            else:
                bm.connect_write(is_stop=stop, on_stage=self.stage.emit)
                bm.erase_and_write_flash(self._image, on_percent=self.percent.emit,
                                         on_stage=self.stage.emit, is_stop=stop)
                self.finished_ok.emit(None)
            return
        flash = _edc15.Edc15Flash(link, log=lambda s: self.stage.emit(s))

        def size_guard(image):
            """Stop before writing a file whose size doesn't match the chip
            the ECU actually has, rather than writing the wrong half."""
            if flash.layout is _edc15.LAYOUT_1M or len(image) == _edc15.LAYOUT_1M.size:
                raise _edc15.KLineError(_edc15.WRITE_1M_UNSUPPORTED)
            if len(image) != flash.layout.size:
                raise _edc15.KLineError(
                    "This ECU has a %s flash, but the file to write is %d KB "
                    "(0x%X). Open a matching %d KB image."
                    % (flash.layout.name, len(image) // 1024, len(image),
                       flash.layout.size // 1024))

        if self._op == "read":
            flash.connect("V", self._speed, loader="flash", is_stop=stop)
            flash.detect_layout(is_stop=stop)
            img = flash.read_flash(on_percent=self.percent.emit,
                                   on_stage=self.stage.emit, is_stop=stop)
            self.finished_ok.emit(img)
        elif self._op == "write":
            flash.connect("V", self._speed, loader="sector", is_stop=stop)
            flash.detect_layout(is_stop=stop)
            size_guard(self._image)
            flash.write_flash_sectors(
                self._image, verify=True, force_full=self._force_full,
                on_percent=self.percent.emit, on_stage=self.stage.emit, is_stop=stop)
            self.finished_ok.emit(None)
        elif self._op == "ee_read":
            flash.connect("V", "Low", loader="eeprom", is_stop=stop)
            self.percent.emit(50)
            ee = flash.read_eeprom(on_stage=self.stage.emit)
            self.finished_ok.emit(ee)
        elif self._op == "irom_read":
            # The sector loader's Cmd23 takes an arbitrary CPU-bus address and the
            # running ECU already has its boot ROM mapped, so this is an ordinary
            # session -- no boot pin, nothing reconfigured.
            flash.connect("V", self._speed, loader="sector", is_stop=stop)
            rom = flash.read_irom(on_percent=self.percent.emit,
                                  on_stage=self.stage.emit, is_stop=stop)
            self.finished_ok.emit(rom)
        elif self._op == "ee_write":
            flash.connect("V", "Low", loader="eeprom", is_stop=stop)
            back, _n = flash.write_eeprom(self._image, on_stage=self.stage.emit,
                                          on_percent=self.percent.emit, is_stop=stop)
            self.finished_ok.emit(back)
        elif self._op == "restore":
            flash_img, ee = self._image
            flash.connect("V", self._speed, loader="sector", is_stop=stop)
            flash.detect_layout(is_stop=stop)
            size_guard(flash_img)
            flash.write_flash_sectors(
                flash_img, verify=True, force_full=self._force_full,
                on_percent=lambda p: self.percent.emit(p * 80 // 100),
                on_stage=self.stage.emit, is_stop=stop)
            for i in range(50):
                if stop():
                    raise _edc15.StopRequested()
                self.stage.emit("Flash written. Waiting %d s before writing EEPROM..." % (10 - i // 5))
                time.sleep(0.2)
            flash.connect("V", "Low", loader="eeprom", is_stop=stop)
            flash.write_eeprom(ee, on_stage=self.stage.emit,
                               on_percent=lambda p: self.percent.emit(80 + p * 20 // 100),
                               is_stop=stop)
            self.finished_ok.emit(None)
        elif self._op == "backup":
            # KWHack full backup: flash first, let the ECU reboot, then EEPROM.
            flash.connect("V", self._speed, loader="flash", is_stop=stop)
            flash.detect_layout(is_stop=stop)
            img = flash.read_flash(on_percent=lambda p: self.percent.emit(p * 90 // 100),
                                   on_stage=self.stage.emit, is_stop=stop)
            for i in range(50):
                if stop():
                    raise _edc15.StopRequested()
                self.stage.emit("Flash read. Waiting %d s before reading EEPROM..." % (10 - i // 5))
                time.sleep(0.2)
            flash.connect("V", "Low", loader="eeprom", is_stop=stop)
            ee = flash.read_eeprom(on_stage=self.stage.emit)
            self.finished_ok.emit((img, ee))


class _PortScanWorker(QtCore.QThread):
    """Runs _can_devices()/kline_devices() off the GUI thread.

    Bug 3a (the ECU dropdown looking permanently disabled after the app is
    closed and reopened -- CAN transport only, "fixed" only by reinstalling)
    was never actually an exception being swallowed: FlashDialog.__init__'s
    restore chain already had a try/except/finally around it (2026-09-21) and
    the symptom kept happening regardless. The shape fits a HANG, not a
    raise -- _refresh_ports() -> _can_devices() ran synchronously on the GUI
    thread, called twice from __init__ whenever the last-used transport
    (persisted via QSettings, which a plain uninstall/reinstall clears --
    explaining why only reinstalling ever "fixed" it) was "can". Nothing in
    _can_devices()'s J2534 registry read, 32-bit-bridge probe subprocess, or
    pyusb OpenPort enumeration is guaranteed to return quickly if the
    driver/bridge was left in a bad state by a previous session -- see bug
    3b, the bridge subprocess hanging after an abrupt disconnect, which is
    structurally the same class of problem. A hang there freezes the whole
    dialog, which looks exactly like "the dropdown is disabled" and does not
    care whether the flash dialog was ever actually opened before -- it only
    needed CAN to have been the last-used transport.

    This fix does not depend on knowing which specific call is slow: whatever
    it is, it now runs here instead of on the GUI thread, so it can no longer
    freeze the dialog. It also logs its own duration to
    flash_dialog_debug.log (only when notably slow) so a reproduction pins
    the exact culprit instead of staying "root cause unconfirmed" forever.
    """
    done = QtCore.Signal(int, list)

    def __init__(self, kind, gen, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._gen = gen

    def run(self):
        t0 = time.monotonic()
        try:
            devs = _can_devices() if self._kind == "can" else kline_devices()
        except Exception:
            devs = []
            _debug_log("EXCEPTION scanning %s devices (gen %d, %.1fs before failing):\n%s"
                       % (self._kind, self._gen, time.monotonic() - t0, traceback.format_exc()))
        else:
            dt = time.monotonic() - t0
            if dt > 1.0:                     # anything under a second is unremarkable
                _debug_log("%s device scan (gen %d) took %.1fs -- see gui_flash.py's "
                           "_can_devices()/j2534.find_bridge_runner()/openport.find_devices() "
                           "for where that time actually went" % (self._kind, self._gen, dt))
        self.done.emit(self._gen, devs)


class FlashDialog(QtWidgets.QDialog):
    """Read from / write to an EDC16 or EDC15 ECU over a K-line cable."""

    def __init__(self, window, mode="read", eeprom_image=None):
        # standalone mode (ovtsuite --flasher) has no main window to be owned by
        super().__init__(getattr(window, "dialog_parent", window))
        self._window = window
        self._standalone = bool(getattr(window, "standalone", False))
        self._worker = None
        self._op = None
        self._eeprom_image = eeprom_image
        try:
            from ..gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        v = QtWidgets.QVBoxLayout(self)

        if self._standalone:
            # No main window to borrow a file from: the file to write is chosen here.
            frow = QtWidgets.QHBoxLayout()
            frow.addWidget(QtWidgets.QLabel("File:"))
            self.file_lbl = QtWidgets.QLabel()
            self.file_lbl.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            frow.addWidget(self.file_lbl, 1)
            self.file_btn = QtWidgets.QPushButton("Open file...")
            self.file_btn.setToolTip("Choose the image to write to the ECU")
            self.file_btn.clicked.connect(self._pick_file)
            frow.addWidget(self.file_btn)
            v.addLayout(frow)
            self._update_file_label()

        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(QtWidgets.QLabel("Transport:"))
        self.transport_box = QtWidgets.QComboBox()
        self.transport_box.addItem("K-line", "kline")
        self.transport_box.addItem("CAN / TP 2.0 (J2534 / OpenPort)", "can")
        self.transport_box.currentIndexChanged.connect(self._transport_changed)
        trow.addWidget(self.transport_box)
        # No CAN-destination-address picker: every CAN-capable ECU has exactly one
        # correct address, fixed per ECU in _CAN_DEST above -- see that table's
        # comment for why a user-facing picker here caused a real bug rather than
        # preventing one (Bear, 2026-09-22).
        self.can_rate = QtWidgets.QComboBox()
        for r in (500000, 250000, 125000, 100000):
            self.can_rate.addItem("%d bit/s" % r, r)
        self.can_rate.setVisible(False)
        trow.addWidget(self.can_rate)
        trow.addStretch(1)
        v.addLayout(trow)

        row = QtWidgets.QHBoxLayout()
        self.port_lbl = QtWidgets.QLabel("Port:")
        row.addWidget(self.port_lbl)
        self.port_box = QtWidgets.QComboBox()
        self._last_port_key = None
        self._refresh_ports()
        self.port_box.currentIndexChanged.connect(self._port_changed)
        row.addWidget(self.port_box, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_ports)
        row.addWidget(refresh)
        pair = QtWidgets.QPushButton("Pair head unit...")
        pair.setToolTip("Add a KW-Dash head unit so its cable appears in this list.")
        pair.clicked.connect(self._pair_head_unit)
        row.addWidget(pair)
        v.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("ECU:"))
        self.ecu_box = QtWidgets.QComboBox()
        self.ecu_box.addItems(["EDC16", "EDC15", "Climatronic", "Cluster", VDO_ECU, "ABS", "ZF6HP", "Aisin TRx"])
        self.ecu_box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.ecu_box.setMinimumContentsLength(7)
        row2.addWidget(self.ecu_box)
        row2.addWidget(QtWidgets.QLabel("  Speed:"))
        self.speed_box = QtWidgets.QComboBox()
        self.speed_box.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.speed_box.setMinimumContentsLength(16)
        row2.addWidget(self.speed_box)
        row2.addStretch(1)
        v.addLayout(row2)

        self.force_full = QtWidgets.QCheckBox()
        # (placed in the options grid below)
        self.boot_mode = QtWidgets.QCheckBox("Boot mode (boot pin bridged; whole-chip write)")
        self.boot_mode.toggled.connect(self._boot_toggled)
        # added to the Advanced group below, not to the main column

        self.staged_chk = QtWidgets.QCheckBox("Run on the head unit")
        self.staged_chk.setToolTip(
            "The head unit runs the whole ECU session itself and reports progress.\n"
            "Closing the laptop's lid mid-flash costs nothing: the job carries on and\n"
            "this end picks the result up when it reconnects.")
        self.staged_chk.toggled.connect(self._staged_toggled)

        # Climatronic-only, WRITE: opt-in selective skip via a deterministic
        # baseline diff. Only sections that differ from the stock the .hvac was
        # saved from are written; unchanged sectors are skipped. No on-module
        # checksum -- it assumes the module currently holds that stock baseline.
        self.hvac_selective = QtWidgets.QCheckBox("Write changed sectors only")
        self.hvac_selective.setToolTip(
            "Write only the sectors you edited, compared with the factory original\n"
            "the set was imported from. Use it only when the module still holds\n"
            "that original. A .sgo picked from disk always writes every sector.")
        self.hvac_selective.setChecked(False)
        self.hvac_selective.setVisible(False)

        # Diagnostic: instead of a normal read, remove the id filter, send ONE TP2.0
        # channel-setup broadcast (to the ECU's fixed address, see _CAN_DEST) and log
        # every frame -- to see if the module answers at the selected CAN rate and on
        # which id. Uses the CAN-rate box for baud (pick 100000 for the C6 comfort
        # CAN). In-process/OpenPort path only.
        self.probe_chk = QtWidgets.QCheckBox("Probe the bus instead of reading")
        self.probe_chk.setToolTip(
            "Read makes no changes and reads nothing: it calls this unit once and\n"
            "lists every CAN message seen for about 2 seconds, to show whether it\n"
            "answers at the selected CAN speed (100000 for the C6 comfort CAN).")
        self.probe_chk.setVisible(False)         # in Advanced (below)

        # VDO cluster write: read every block back afterwards (in Advanced)
        self.vdo_verify_chk = QtWidgets.QCheckBox(
            "Verify the cluster write by reading it back")
        self.vdo_verify_chk.setToolTip(
            "After writing the cluster EEPROM, read all of it back and compare. "
            "Takes as long again as the write.\nOff: each block is still "
            "confirmed by the cluster's own reply as it is written.")
        try:
            _s = QtCore.QSettings(*branding.QSETTINGS)
            self.vdo_verify_chk.setChecked(
                _as_bool(_s.value("flash/vdo_verify", False), False))
        except Exception:
            pass
        self.vdo_verify_chk.toggled.connect(self._vdo_verify_toggled)
        self.vdo_verify_chk.setVisible(False)    # in Advanced (below)
        self.fast_disable_chk = QtWidgets.QCheckBox("Disable Fast Init")
        self.fast_disable_chk.setChecked(False)      # fast init is the K-line default
        self.fast_disable_chk.setToolTip(
            "By default K-line uses ISO 14230 fast init (break pulse on a serial cable; "
            "hardware on a J2534 device), which the later CAN-init-only EDC16 ECUs need. "
            "Check this to force the 5-baud slow init instead (older K-line-init ECUs).")
        # ZF6HP over UDS (6HP28) only -- the counterpart of Force full write,
        # which applies over TP 2.0 only. _zf_options_sync shows whichever one
        # the next write would use.
        self.uds_cal_only_chk = QtWidgets.QCheckBox("Calibration only")
        self.uds_cal_only_chk.setToolTip(
            "6HP28: from a full 2 MB image, write only the calibration and leave "
            "the program as it is.")
        self.uds_cal_only_chk.setVisible(False)
        # One fixed block of options, whichever of them the unit shows: a hidden
        # option keeps its room, so the window never changes shape. Force full
        # write, Calibration only and Write changed sectors only never apply to
        # the same unit, so they share a cell.
        opts = QtWidgets.QGridLayout()
        opts.setContentsMargins(0, 0, 0, 0)
        for w in (self.force_full, self.uds_cal_only_chk, self.hvac_selective):
            opts.addWidget(w, 0, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        opts.addWidget(self.staged_chk, 0, 1)
        opts.addWidget(self.fast_disable_chk, 1, 0)
        opts.setColumnStretch(2, 1)
        self.options_grid = opts
        v.addLayout(opts)
        _dlgbase.retain_space(self.force_full, self.uds_cal_only_chk,
                              self.hvac_selective, self.staged_chk,
                              self.fast_disable_chk, self.can_rate)

        # (EDC16 Read full / Write full ask for their blocks when chosen:
        # gui_edc16_blocks)

        srow = QtWidgets.QHBoxLayout()
        self.stage = QtWidgets.QLabel("Ready.")
        self.stage.setWordWrap(True)
        self.stage.setStyleSheet("font-weight:600")
        _dlgbase.reserve_lines(self.stage, 2)        # a text swap never resizes the window
        srow.addWidget(self.stage, 1)
        self.timer_lbl = QtWidgets.QLabel("")
        self.timer_lbl.setStyleSheet("font-weight:600; font-family:monospace")
        self.timer_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                                    | QtCore.Qt.AlignmentFlag.AlignVCenter)
        srow.addWidget(self.timer_lbl)
        v.addLayout(srow)
        self.bar = _dlgbase.PercentBar()             # shows its % on macOS too
        self.bar.setRange(0, 100)
        v.addWidget(self.bar)

        self._t0 = None                              # operation-timer start (monotonic)
        self._last_elapsed = None                    # elapsed seconds of the last operation
        self._op_timer = QtCore.QTimer(self)
        self._op_timer.setInterval(200)
        self._op_timer.timeout.connect(self._tick_timer)

        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:gray")
        _dlgbase.reserve_lines(self.note, 5)         # the longest note fits
        self.note.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        v.addWidget(self.note)

        btns = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read flash")
        self.read_btn.clicked.connect(lambda: self._start("read"))
        btns.addWidget(self.read_btn)
        self.write_btn = QtWidgets.QPushButton("Write flash")
        self.write_btn.clicked.connect(lambda: self._start("write"))
        btns.addWidget(self.write_btn)
        # EDC16-only: Read full (non-destructive) and Write full (ERASES and
        # rewrites; recover via BDM if it fails), each asking for its blocks
        # first (gui_edc16_blocks) and guarded by a stern confirm for a write.
        self.prog_readfull_btn = QtWidgets.QPushButton("Read full...")
        self.prog_readfull_btn.setToolTip(
            "Read the blocks you pick (program, calibration, internal flash) into "
            "a project, as one set. Changes nothing.")
        self.prog_readfull_btn.clicked.connect(lambda: self._start("read_program_full"))
        self.prog_readfull_btn.setVisible(False)
        self.prog_arm_btn = QtWidgets.QPushButton("Write full...")
        self.prog_arm_btn.setToolTip(
            "Erase and rewrite the blocks you pick from the file in front. A block "
            "the ECU already holds is skipped, unless Force full write is ticked. "
            "If this fails, the ECU needs a bench (BDM) recovery.")
        self.prog_arm_btn.clicked.connect(lambda: self._start("write_program"))
        self.prog_arm_btn.setVisible(False)
        # EDC16 research probes: built by gui_edc16, placed in Advanced below and
        # shown only when gui_edc16.RESEARCH_TOOLS is on.
        _gui16.make_research_widgets(self)
        self.ee_read_btn = QtWidgets.QPushButton("Read EEPROM")
        self.ee_read_btn.clicked.connect(lambda: self._start("ee_read"))
        self.ee_write_btn = QtWidgets.QPushButton("Write EEPROM...")
        self.ee_write_btn.clicked.connect(lambda: self._start("ee_write"))
        self.irom_btn = QtWidgets.QPushButton("Read internal ROM")
        self.irom_btn.setToolTip(
            "Read the C167's own 32 KB boot ROM (CPU 0x000000-0x007FFF), which holds the\n"
            "shared library the flash calls into. No boot mode and no boot pin: the running\n"
            "ECU already has it mapped, and the sector loader reads any address.\n"
            "The dump is checked for the silent every-other-byte-doubled read before saving.")
        self.irom_btn.clicked.connect(lambda: self._start("irom_read"))
        self._irom_tip = self.irom_btn.toolTip()     # restored when boot mode is off
        self.backup_btn = QtWidgets.QPushButton("Read flash + EEPROM")
        self.backup_btn.setToolTip("Read the flash and the EEPROM into a project (one set holding both)")
        self.backup_btn.clicked.connect(lambda: self._start("backup"))
        self.restore_btn = QtWidgets.QPushButton("Write flash + EEPROM")
        self.restore_btn.setToolTip("Write the loaded backup's flash (changed sectors) and EEPROM")
        self.restore_btn.clicked.connect(lambda: self._start("restore"))
        btns.addStretch(1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        btns.addWidget(self.cancel_btn)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        v.addLayout(btns)
        # Read and Write keep their slots (and a width that holds each of their
        # labels, or the unit's own); the other operations sit in a block of four
        # fixed slots under them. EDC16's Read / Write full and EDC15's EEPROM
        # pair share the first two slots (never on the same unit), and a hidden
        # button keeps its room, so no unit's choice moves anything.
        _dlgbase.fit_width(self.read_btn, ("Read flash", "Read calibration", "Read EEPROM"))
        _dlgbase.fit_width(self.write_btn, ("Write flash", "Write calibration", "Write EEPROM"))
        xg = QtWidgets.QGridLayout()
        xg.setContentsMargins(0, 0, 0, 0)
        xg.addWidget(self.ee_read_btn, 0, 0)
        xg.addWidget(self.prog_readfull_btn, 0, 0)
        xg.addWidget(self.ee_write_btn, 0, 1)
        xg.addWidget(self.prog_arm_btn, 0, 1)
        xg.addWidget(self.backup_btn, 0, 2)
        xg.addWidget(self.restore_btn, 0, 3)
        xg.setColumnStretch(4, 1)
        self.extras_grid = xg
        v.addLayout(xg)
        _dlgbase.retain_space(self.read_btn, self.write_btn, self.ee_read_btn,
                              self.ee_write_btn, self.prog_readfull_btn,
                              self.prog_arm_btn, self.backup_btn, self.restore_btn)

        # --- Advanced -----------------------------------------------------
        # A disclosure, not a setting: an arrow QToolButton that expands a plain
        # container. A checkable QGroupBox was the first try and reads wrong --
        # its checkbox looks like an option, and collapsed it still draws an empty
        # frame that costs height for nothing. Collapsed, this is one ordinary
        # row; expanded, an indented block.
        #
        # The controls here are the ones that are rarely the right answer: boot
        # mode, the internal-ROM read, and the EDC16 research probes (shown only
        # with gui_edc16.RESEARCH_TOOLS). Each keeps its own conditional
        # visibility (`irom_btn` is EDC15-only, the probes EDC16-only), combined
        # with the expanded state in `_adv_visible` rather than one overwriting
        # the other.
        self.adv_toggle = QtWidgets.QToolButton()
        self.adv_toggle.setText("Advanced")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setChecked(False)
        self.adv_toggle.setAutoRaise(True)
        self.adv_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.adv_toggle.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_toggle.setToolTip(
            "Diagnostics and recovery. Nothing here is needed for an ordinary "
            "read, write or backup.")
        self.adv_toggle.toggled.connect(self._adv_toggled)
        arow = QtWidgets.QHBoxLayout()
        arow.addWidget(self.adv_toggle)
        arow.addStretch(1)
        v.addLayout(arow)

        self.adv_panel = QtWidgets.QWidget()
        abox = QtWidgets.QVBoxLayout(self.adv_panel)
        abox.setContentsMargins(16, 0, 0, 4)     # indented under the arrow
        abox.addWidget(self.boot_mode)
        advb = QtWidgets.QHBoxLayout()           # buttons at natural width,
        advb.addWidget(self.irom_btn)            # like the main row above
        advb.addWidget(self.prog_bootprobe_btn)
        advb.addWidget(self.prog_cksumprobe_btn)
        advb.addWidget(self.prog_seedprobe_btn)
        advb.addStretch(1)
        abox.addLayout(advb)
        # The rest of the EDC16 diagnostic probes: once on the main button row,
        # they made the dialog ~2100 px wide for EDC16. Two more rows here, each
        # empty (and so taking no height) for every other ECU.
        advr = QtWidgets.QHBoxLayout()
        advr.addWidget(self.prog_ramread_btn)
        advr.addWidget(self.prog_immoread_btn)
        advr.addWidget(self.prog_findeep_btn)
        advr.addStretch(1)
        abox.addLayout(advr)
        # Cluster / ABS troubleshooting aid
        advc = QtWidgets.QHBoxLayout()
        advc.addWidget(self.probe_chk)
        advc.addWidget(self.vdo_verify_chk)
        advc.addStretch(1)
        abox.addLayout(advc)
        advg = QtWidgets.QHBoxLayout()           # Scanmatik SM2 GPT test + its options
        advg.addWidget(self.prog_gptverify_btn)
        advg.addWidget(self.prog_gptverify_find_btn)
        advg.addWidget(self.gpt_crank_only_chk)
        advg.addWidget(self.gpt_gap_chk)
        advg.addWidget(self.gpt_force_ee_chk)
        advg.addStretch(1)
        abox.addLayout(advg)
        # ZF6HP: the flash transport. Automatic is right for almost everyone;
        # forcing one is for a TCU the automatic choice gets wrong.
        self.zf_transport_row = QtWidgets.QWidget()
        zrow = QtWidgets.QHBoxLayout(self.zf_transport_row)
        zrow.setContentsMargins(0, 0, 0, 0)
        zrow.addWidget(QtWidgets.QLabel("Transport:"))
        self.zf_transport_box = QtWidgets.QComboBox()
        for _val, _lbl in ZF6HP_TRANSPORTS:
            self.zf_transport_box.addItem(_lbl, _val)
        self.zf_transport_box.setToolTip(
            "Automatic reads over TP 2.0 and moves to UDS if the TCU does not "
            "answer,\nand writes over the transport the open file needs. Pick one "
            "to use only that.")
        self.zf_transport_box.currentIndexChanged.connect(self._zf_transport_changed)
        zrow.addWidget(self.zf_transport_box)
        zrow.addStretch(1)
        self.zf_transport_row.setVisible(False)
        abox.addWidget(self.zf_transport_row)
        self.adv_panel.setVisible(False)
        v.addWidget(self.adv_panel)
        v.addStretch(1)       # spare height goes below everything: nothing above moves

        self._adv_children = (self.boot_mode, self.irom_btn,
                              self.prog_bootprobe_btn, self.prog_cksumprobe_btn,
                              self.prog_seedprobe_btn, self.prog_ramread_btn,
                              self.prog_immoread_btn, self.prog_findeep_btn,
                              self.prog_gptverify_btn, self.prog_gptverify_find_btn,
                              self.gpt_crank_only_chk, self.gpt_gap_chk,
                              self.gpt_force_ee_chk,
                              self.probe_chk, self.vdo_verify_chk,
                              self.zf_transport_row)
        self._adv_sync()
        self._init_size()

        self.ecu_box.currentIndexChanged.connect(self._ecu_changed)
        self.ecu_box.currentIndexChanged.connect(self._ecu_picked)
        try:
            self._ecu_changed()
            self._restore_settings()                 # last-used selections become the defaults
            # ...except the module: an open file's platform decides that, and the
            # saved choice only applies when there is no file to go by.
            self._default_ecu_from_file()
            self._apply_staged_default()              # after restore: the port decides this
        except Exception:
            # Bug 3a: the ECU dropdown (and friends) have been seen stuck disabled
            # right after opening this dialog, no operation ever attempted, only
            # after CAN/J2534 was used previously (so _restore_settings() replays a
            # transport of "can", which fires _transport_changed()/_ecu_changed()
            # via a native Qt signal crossing rather than a plain call). Whatever
            # goes wrong in there, log it -- this build has no console, so without
            # this the failure leaves no trace anywhere -- and self-heal below
            # rather than leave the dialog half-broken.
            _debug_log("EXCEPTION during ecu/transport restore in FlashDialog.__init__:\n"
                       + traceback.format_exc())
        finally:
            # Nothing here should ever be disabled -- no operation has started yet.
            for w in (self.ecu_box, self.port_box, self.speed_box, self.transport_box,
                      self.can_rate):
                w.setEnabled(True)
            self._boot_toggled()
            self._transport_changed()                # re-derive the real enabled/visible state
        if mode == "write":
            self.write_btn.setDefault(True)

    # ---- the file this dialog writes ----
    def _default_ecu_from_file(self):
        """Select the module that flashes the loaded file's platform (EDC15,
        EDC16, ZF6HP, Climatronic). Returns False, changing nothing, when no file
        is loaded or its platform has no flasher module."""
        from .standalone import ecu_for_platform
        prim = getattr(self._window, "primary", None)
        ecu = ecu_for_platform(getattr(prim, "platform", None)) if prim is not None else None
        if not ecu or self.ecu_box.findText(ecu) < 0:
            return False
        if ecu == "EDC15" and self._is_can():        # K-line only: leave CAN first
            i = self.transport_box.findData("kline")
            if i >= 0:
                self.transport_box.setCurrentIndex(i)
        self.ecu_box.setCurrentText(ecu)             # CAN-only modules switch transport themselves
        return True

    def _update_file_label(self):
        prim = getattr(self._window, "primary", None)
        if prim is None:
            self.file_lbl.setText("No file chosen")
            self.file_lbl.setToolTip("")
            return
        name = getattr(getattr(prim, "binfile", None), "name", "") or ""
        self.file_lbl.setText(name or getattr(prim, "path", "") or "(loaded)")
        self.file_lbl.setToolTip(getattr(prim, "path", "") or "")

    def _pick_file(self):
        from .. import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open the image to write", _recent.get_last_dir(),
            "Projects and images (*.ovt *.bin *.ori *.sgo *.dim *.frf *.edc16 "
            "*.edc15 *.6hp *.hvac *.edc15bak);;OVT projects (*.ovt);;"
            "All files (*)")
        if path:
            self.load_file(path)

    def load_file(self, path):
        """Standalone: load ``path`` as the file to write and retarget the module."""
        if not self._window.open_path(path, self):
            return False
        self._update_file_label()
        self._default_ecu_from_file()
        self._ecu_changed()
        return True

    # ---- UI state ----
    def _ecu(self):
        return self.ecu_box.currentText()

    def _ecu_changed(self):
        edc15 = self._ecu() == "EDC15"
        # Climatronic, Cluster and ZF6HP share one UI shape: CAN / TP 2.0 only,
        # no EEPROM/backup/boot/staged. Climatronic/Cluster write is not
        # implemented; ZF6HP write IS implemented (see write_btn enable logic
        # below, which carves ZF6HP back out of the read-only set). The bus-probe
        # / CAN-log debug aids are NOT part of this shared shape any more --
        # ZF6HP dropped them (2026-09-22, see the diag_ecu check further down) --
        # they stay Climatronic/Cluster-only now.
        clim = self._ecu() in CLIM_SHAPE_ECUS
        if self._ecu() in KLINE_ONLY_ECUS and self._is_can():
            i = self.transport_box.findData("kline")
            if i >= 0:
                self.transport_box.setCurrentIndex(i)   # -> _transport_changed
        if self._ecu() in CAN_ONLY_ECUS and not self._is_can():
            i = self.transport_box.findData("can")
            if i >= 0:
                self.transport_box.setCurrentIndex(i)   # -> _transport_changed
                # Bug fix (2026-09-21): this used to just `return` here, on the
                # assumption that switching transport was the only thing this
                # branch needed to do. But _transport_changed() does NOT call
                # _ecu_changed() back, so everything BELOW this early return --
                # can_dest, window title, force_full/note text, and critically
                # the Advanced-panel checkboxes' visibility -- never ran for whichever ECU
                # triggered this branch. Harmless when the user manually picks
                # a clim ECU from a non-CAN state (they'd naturally reselect it
                # or the visible effect was never checked), but _restore_settings()
                # hitting this exact path on dialog open (ECU restored to ZF6HP
                # while transport was still at its pre-restore kline default)
                # left them permanently hidden until the user manually
                # switched ECU away and back -- exactly what unmasked this: two
                # ECU selections in a row skips the early-return the SECOND time
                # (transport is already "can" by then), which is why that
                # "worked". Recursing here instead of returning re-runs this
                # method now that the transport is actually "can", so it falls
                # through to the rest of the setup below on the SAME dialog-open
                # pass instead of needing a second manual selection.
                self._ecu_changed()
            return
        self.setWindowTitle("ECU flashing (%s)" % self._ecu())
        self._rebuild_speeds()
        self._apply_fast_init_default()              # EDC15 -> slow init default
        self._apply_staged_default()                 # staged is EDC16-only for now
        self.ee_read_btn.setVisible(edc15)
        self.ee_write_btn.setVisible(edc15)
        self._adv_visible(self.irom_btn, edc15)
        self.backup_btn.setVisible(edc15)
        has_ee = self._window_eeprom() is not None
        self.restore_btn.setVisible(edc15 and has_ee)
        held = self._held_eeprom() is not None
        self.ee_write_btn.setText("Write EEPROM" if held else "Write EEPROM...")
        self.ee_write_btn.setToolTip(
            "Write the EEPROM image already loaded (backup / editor)" if held
            else "Choose a 512-byte EEPROM image file and write it")
        self._adv_visible(self.boot_mode, edc15)
        if not edc15:
            self.boot_mode.setChecked(False)
        # EDC15 is ALWAYS 5-baud slow init -- no fast-init option shown at all.
        self.fast_disable_chk.setVisible(not edc15 and not self._is_can())
        self._boot_toggled()
        if edc15:
            self.force_full.setText("Force full write")
            self.force_full.setToolTip(
                "Rewrite all 11 sectors, even the ones that already match.")
            self.note.setText(
                "Read goes into a project (this one or a new one). Write reprograms only "
                "the changed sectors, then verifies. <b>Do not disconnect during a write.</b>")
        elif self._ecu() == "ZF6HP":
            self.force_full.setText("Force full write")
            self.force_full.setToolTip(
                "6HP19 / 6HP26 (TP 2.0): rewrite both areas, even one that "
                "already matches.")
        else:
            self.force_full.setText("Force full write")
            self.force_full.setToolTip(
                "Rewrite every block, even the ones that already match.")
            self.note.setText(
                "Read goes into a project (the 512 KB calibration region). Write "
                "reprograms the changed blocks. <b>Do not disconnect during a write.</b>")
        # Climatronic/Cluster write is not implemented; ZF6HP write IS implemented
        # (security key, transferData cipher, and write_areas() mechanics are all
        # solved -- see ovtsuite.flash.zf6hp) even though it shares the rest of
        # the "clim" UI shape (CAN-only, no EEPROM/backup/boot/staged).
        # Climatronic write is now IMPLEMENTED (car-captured ODIS sequence, see
        # climatronic.flash_write); only the Cluster stays read-only. force_full
        # stays hidden for both (the HVAC writer always writes all blocks).
        read_only_clim = self._ecu() in ("Climatronic", "Cluster", "ABS", "Aisin TRx")   # visual shape
        cluster_ro = self._ecu() in ("Cluster", "ABS", "Aisin TRx")   # actually read-only (write not yet confirmed)
        self.force_full.setVisible(not read_only_clim)
        # Experimental selective-skip: Climatronic only, and only when writing.
        # Selective-skip: shown for a Climatronic WRITE. It now uses a
        # deterministic baseline diff (only sections edited vs the stock the
        # .hvac was saved from are written) -- no on-module checksum. Only a
        # .hvac carries the baseline; a .sgo falls back to a full write.
        self.hvac_selective.setVisible(self._ecu() == "Climatronic" and not held)
        self.staged_chk.setVisible(not clim)
        # Bus-probe and CAN-traffic-log are reverse-engineering aids, not day-to-day
        # flashing tools -- useful while Climatronic/Cluster support is still being
        # bench-verified (both remain read-only), but ZF6HP's read/write protocol is
        # fully solved, bench-verified and speed-tuned (see zf6hp-flash-transport.md /
        # zf6hp-write-speed.md project notes), so it no longer shows either one.
        # The area probe is a ZF6HP reverse-engineering aid: a normal read covers
        # only the calibration window (READ_AREAS), which carries no program code,
        # so this asks the TCU whether the code areas are readable at all.
        # ZF6HP covers the 6HP19/26 (KWP2000 over TP 2.0) AND the 6HP28 (UDS):
        # one Read / Write pair, the transport picked automatically (see
        # _zf_write_choice and _Worker._run_zf6hp) unless forced in Advanced.
        zf = self._ecu() == "ZF6HP"
        self._adv_visible(self.zf_transport_row, zf)
        self._zf_options_sync()
        self._zf_keep_visible()
        self.read_btn.setToolTip(
            "Read the calibration. Tries TP 2.0 (6HP19 / 6HP26) first, then UDS "
            "(6HP28). Changes nothing." if zf else "")
        # Climatronic: no OBD read exists (the module only takes a write).
        self.read_btn.setVisible(self._ecu() != "Climatronic")
        self.write_btn.setVisible(True)
        diag_ecu = self._ecu() in ("Cluster", "ABS")
        self._adv_visible(self.probe_chk, diag_ecu)
        self._adv_visible(self.vdo_verify_chk, self._ecu() == VDO_ECU)
        if not diag_ecu:
            self.probe_chk.setChecked(False)
        if clim:                                    # staged path must never fire for HVAC
            self.staged_chk.setChecked(False)
            self.staged_chk.setEnabled(False)
        self.write_btn.setEnabled(not cluster_ro)
        self.write_btn.setToolTip(
            "This module is read-only in this version." if cluster_ro else
            "Climatronic: flash the set in front, or a chosen .sgo (programming session)." if self._ecu() == "Climatronic"
            else "Write the open file, over TP 2.0 or UDS as the file needs." if zf
            else "")
        is_edc16 = self._ecu() == "EDC16"
        self.prog_arm_btn.setVisible(is_edc16)
        self.prog_readfull_btn.setVisible(is_edc16)
        for _name in _gui16.RESEARCH_WIDGETS:
            self._adv_visible(getattr(self, _name), _gui16.research_visible(is_edc16))
        # EDC16: the plain Read / Write move the calibration only
        self.read_btn.setText("Read calibration" if is_edc16 else "Read flash")
        self.write_btn.setText("Write calibration" if is_edc16 else "Write flash")
        if clim:
            # the Aisin TRx on K-line keeps the init choice and the speed list
            tcu_kline = self._ecu() in EITHER_BUS_TCU and not self._is_can()
            self.speed_box.setEnabled(tcu_kline)
            self.fast_disable_chk.setVisible(tcu_kline)
            if self._ecu() == "Climatronic":
                self.note.setText(
                    "Write flashes the Climatronic set in front, or a chosen .sgo (this "
                    "module can't be read over OBD). <b>Do not disconnect during a "
                    "write.</b>")
            elif self._ecu() == "Cluster":
                self.note.setText(
                    "Read goes into a project (the cluster's flash). Experimental: the "
                    "cluster may refuse the read. Writing is not available.")
            elif self._ecu() == "ABS":
                self.note.setText(
                    "Bosch ESP8 (C6 A6). Read goes into a project (the module's flash). "
                    "Writing is not available.")
            elif zf:
                self.note.setText(
                    "ZF 6HP19, 6HP26 and 6HP28. Read finds the TCU by itself; Write "
                    "picks TP 2.0 or UDS from the open file. <b>Do not disconnect "
                    "during a write.</b>")
            elif self._ecu() == "Aisin TRx":
                self.note.setText(
                    "Aisin 09D, over CAN / TP 2.0 (1 MB flash) or K-line (5-baud init at "
                    "0x02; 1 MB or 512 KB flash, found automatically). K-line Speed: Low stays "
                    "at 10400 (a full read takes about 50 minutes); Medium / High ask the TCU "
                    "to go faster and fall back to 10400 if it or the cable cannot. Read goes "
                    "into a project. "
                    "Writing is not available yet. The TR-80SD (0C8/09G) is not supported.")
            else:
                self.note.setText(
                    "Read goes into a project (the calibration). Write rewrites only "
                    "the areas that changed, then verifies. <b>Do not disconnect "
                    "during a write.</b>")

        if self._ecu() == VDO_ECU:
            self._vdo_ui()
        if hasattr(self, "_adv_base"):
            self._adv_refit()                       # Advanced open: its rows changed

    # ---- ZF6HP: one entry, two transports ----
    def _zf_override(self):
        """The Advanced Transport choice: "auto", "tp20" or "uds"."""
        box = getattr(self, "zf_transport_box", None)
        return (box.currentData() if box is not None else None) or "auto"

    def _zf_forced(self):
        return self._ecu() == "ZF6HP" and self._zf_override() != "auto"

    def _zf_write_choice(self):
        """The transport a ZF6HP write would use, as a zf6hp_uds.TransportChoice:
        the Advanced override when one is forced, else what the open file's
        structural identity says (transport_for_session: container meta, KB
        family, controller MCU, and only then a size guess, marked "low").
        None when there is no file to go by."""
        from . import zf6hp_uds as _uds
        forced = self._zf_override()
        if forced != "auto":
            return _uds.TransportChoice(forced, "forced", "set in Advanced")
        prim = getattr(self._window, "primary", None)
        if prim is None:
            return None
        try:
            return _uds.transport_for_session(prim)
        except Exception:                       # noqa: BLE001 - never block the dialog
            return None

    def _zf_options_sync(self):
        """Show the write option the next ZF6HP write can use: Force full write
        over TP 2.0, Calibration only over UDS. With no file open (Automatic)
        there is nothing to write yet, so the dialog keeps its everyday shape --
        Force full write, the 6HP19/26 option -- rather than an empty column;
        it switches the moment a 6HP28 file is opened or UDS is forced."""
        if not hasattr(self, "uds_cal_only_chk"):
            return
        if self._ecu() != "ZF6HP":
            self.uds_cal_only_chk.setVisible(False)
            return
        choice = self._zf_write_choice()
        uds = choice is not None and choice.is_uds
        self.force_full.setVisible(not uds)
        self.uds_cal_only_chk.setVisible(uds)

    def _zf_keep_visible(self):
        """A forced transport changes what Read and Write do, so (like boot mode
        and the bus probe) it is never left armed inside a collapsed Advanced."""
        toggle = getattr(self, "adv_toggle", None)
        if toggle is not None and self._zf_forced() and not toggle.isChecked():
            toggle.setChecked(True)

    def _zf_transport_changed(self, *_):
        self._zf_options_sync()
        self._zf_keep_visible()

    def _vdo_verify_toggled(self, on):
        try:
            s = QtCore.QSettings(*branding.QSETTINGS)
            s.setValue("flash/vdo_verify", bool(on))
        except Exception:
            pass

    def _vdo_ui(self):
        """The VDO cluster's shape: K-line only, always 5-baud init at 10400,
        read and write of the 2 KB EEPROM, nothing else."""
        if self._is_can():
            i = self.transport_box.findData("kline")
            if i >= 0:
                self.transport_box.setCurrentIndex(i)
        self.speed_box.setEnabled(True)
        self.fast_disable_chk.setVisible(False)
        self.force_full.setVisible(False)
        self.staged_chk.setChecked(False)
        self.staged_chk.setVisible(False)
        self.write_btn.setEnabled(True)
        self.read_btn.setText("Read EEPROM")         # the cluster has an EEPROM, no flash
        self.write_btn.setText("Write EEPROM")
        self.write_btn.setToolTip("Write the cluster EEPROM that is open in %s." % branding.APP_NAME)
        self.note.setText(
            "Mk4-era VDO cluster. Read goes into a project (its 2 KB EEPROM). Write "
            "writes the cluster EEPROM in front. Ignition on, engine off. Speed: High "
            "sends one command per block (about twice as fast); Low is the "
            "standard dialog.")

    def _rebuild_speeds(self):
        """Populate the speed list for the current ECU, dropping High when the
        selected cable can't hold it (CH340/CH341 or a J2534 device -> Medium cap)."""
        edc15 = self._ecu() == "EDC15"
        aisin = self._ecu() == "Aisin TRx"
        vdo = self._ecu() == VDO_ECU
        labels = list(VDO_SPEEDS if vdo else AISIN_SPEEDS if aisin
                      else EDC15_SPEEDS if edc15 else EDC16_SPEEDS)
        keys = list(VDO_SPEED_KEYS if vdo else SPEED_KEYS if (edc15 or aisin)
                    else EDC16_SPEED_KEYS)
        if not edc15 and not vdo and not cable_allows_high(self.port_box.currentData()):
            labels = labels[:-1]                     # drop High (244898) for capped cables
            keys = keys[:-1]
        prev = self.speed_box.currentData()
        if aisin and getattr(self, "_speeds_for", None) != "Aisin TRx":
            prev = "Low"                             # a fresh pick starts at 10400
        elif not aisin and getattr(self, "_speeds_for", None) == "Aisin TRx":
            prev = None                              # the Aisin's Low is not the others' Low
        if vdo != (getattr(self, "_speeds_for", None) == VDO_ECU):
            prev = "High" if vdo else None           # the cluster's list is its own
        self._speeds_for = self._ecu()
        self.speed_box.blockSignals(True)
        self.speed_box.clear()
        for lbl, key in zip(labels, keys):
            self.speed_box.addItem(lbl, key)
        idx = next((i for i in range(self.speed_box.count())
                    if self.speed_box.itemData(i) == prev),
                   keys.index("Medium") if "Medium" in keys else 0)
        self.speed_box.setCurrentIndex(min(idx, self.speed_box.count() - 1))
        self.speed_box.blockSignals(False)

    def _boot_toggled(self, *_):
        self._apply_staged_default()                 # boot mode has no staged job
        boot = self.boot_mode.isChecked() and self._ecu() == "EDC15"
        # boot mode has no speed negotiation / sector logic and cannot reach the EEPROM
        self.speed_box.setEnabled(not boot and self._speed_applies())
        self.force_full.setEnabled(not boot)
        for w in (self.ee_read_btn, self.ee_write_btn, self.backup_btn, self.restore_btn,
                  self.irom_btn):
            w.setEnabled(not boot)
        # ...and say why it is greyed out, restoring the real tooltip afterwards
        # (it used to keep the boot-mode message for the rest of the session)
        self.irom_btn.setToolTip(
            "The internal-ROM read uses the normal K-line session; untick boot "
            "mode." if boot else self._irom_tip)

    def _transport_kind(self):
        tb = getattr(self, "transport_box", None)
        return tb.currentData() if tb is not None else "kline"

    #: control units whose K-line speed is the user's choice (the rest talk at
    #: a rate of their own: the VDO cluster 10400, the TCUs/HVAC over CAN)
    _SPEED_ECUS = ("EDC16", "EDC15", VDO_ECU) + EITHER_BUS_TCU

    def _speed_applies(self) -> bool:
        """The Speed list means something for the control unit picked (and
        the transport): K-line EDC15 / EDC16. One rule, so a finished job
        (``_busy(False)``) does not switch the list back on for a unit that
        never offered it."""
        return self._ecu() in self._SPEED_ECUS and not self._is_can()

    def _is_can(self):
        return self._transport_kind() == "can"

    def _transport_changed(self, *_):
        kind = self._transport_kind()
        can = kind == "can"
        self.port_lbl.setText("Device:" if can else "Port:")
        self.can_rate.setVisible(can)
        # the module follows the transport here: it keeps the transport just
        # picked, not the one it remembers (_ecu_picked)
        self._ecu_from_transport = True
        try:
            if can:
                if self._ecu() in KLINE_ONLY_ECUS:
                    self.ecu_box.setCurrentText("EDC16")
            elif self._ecu() in CAN_ONLY_ECUS:
                self.ecu_box.setCurrentText("EDC16")
        finally:
            self._ecu_from_transport = False
        self.ecu_box.setEnabled(True)               # EDC16 is the one ECU on both transports
        self.speed_box.setEnabled(not can and self._speed_applies())
        self.fast_disable_chk.setVisible(                       # K-line, EDC16 / Aisin TRx
            not can and self._ecu() not in ("EDC15", VDO_ECU))
        # Ask the upcoming rescan for the cable this (controller, transport)
        # last used. The scan is off-thread, so this is the same hand-off
        # _restore_settings() uses rather than a synchronous match.
        want = self._want_port_for(self._ecu(), kind)
        if want:
            self._pending_port_repr = want
        self._refresh_ports()                       # rebuilds ports -> _port_changed

    def _port_changed(self, *_):
        """After the K-line port changes, re-apply the fast-init default and rebuild
        the speed list (High hidden on a CH340/J2534 Medium-capped cable)."""
        self._remember_port()
        if not hasattr(self, "fast_disable_chk") or not hasattr(self, "speed_box"):
            return                                   # still building the dialog
        if self._is_can():
            return
        self._apply_fast_init_default()
        self._apply_staged_default()
        self._rebuild_speeds()
        if self._ecu() == VDO_ECU:
            self._vdo_ui()                           # fixed 10400 / 5-baud, no options

    def _staged_toggled(self, *_):
        """Staged runs on the head unit, so nothing on this end drives the link."""
        if not hasattr(self, "fast_disable_chk"):
            return
        staged = self.staged_chk.isChecked() and self.staged_chk.isEnabled()
        # The init strategy is read from the image by the descriptor builder, so the
        # fast-init checkbox has nothing to say about a staged job.
        self.fast_disable_chk.setEnabled(not staged and self._can_choose_fast_init())

    def _can_choose_fast_init(self):
        if self._is_can():
            return False
        return self._ecu() == "EDC15" or not is_j2534(self.port_box.currentData())

    def _staged_available(self):
        """(available, why not) for the current device, ECU and operation.

        READS ARE STAGED TOO, and the first version of this was wrong to treat them as
        the safe case. Staged mode is not only about surviving an interrupted erase; it
        is about keeping the network out of a conversation that has the ECU's own timing
        in it. A 512 KB read is ~2100 SOLICITED packets -- one request per 254 bytes,
        each sitting in the controller's reply window -- so thin means ~4200 round trips
        inside a live session, and one hiccup past it drops the session and the read.

        For an EDC15 it is not a risk but an impossibility: after the loader starts the
        framing is a plain length byte, and `RemoteKLine` assembles outgoing bytes into
        KWP2000 frames where that byte's top bit means "addressed". Anything longer than
        0x3F is mis-framed. The short commands line up by coincidence, which is exactly
        why the thin path looks like it works.
        """
        device = self.port_box.currentData()
        if not is_remote(device):
            return False, "Only a paired head unit can run a job on its own."

        # What the head unit has an executor for. Checked here rather than left to fail
        # later: `_run_staged` would otherwise fall through to a branch that builds the
        # wrong job out of whatever the button handed it.
        if self._op in ("ee_read", "ee_write") and self._ecu() != "EDC15":
            return False, "Only an EDC15 has an EEPROM behind this cable."

        if self._op in ("backup", "restore") and self._ecu() != "EDC15":
            return False, "Read flash + EEPROM is for an EDC15: its flash and its EEPROM."

        if self._op not in (None, "read", "write", "ee_read", "ee_write",
                            "backup", "restore"):
            return False, "The head unit has no job for this operation."

        if self.boot_mode.isChecked() and self._ecu() == "EDC15":
            # Boot mode IS staged now, and it is the one that most needs to be: nothing
            # in it is framed, so thin mode never sends even the auto-baud byte, and the
            # failure looks exactly like a boot pin that is not bridged.
            if self._op in (None, "read", "write"):
                return True, ""
            return False, "Boot mode does flash reads and writes; it cannot reach the EEPROM."

        if self._ecu() in ("EDC16", "EDC15"):
            return True, ""
        return False, "The head unit has no executor for this controller."

    def _apply_staged_default(self):
        """Offer staged where it exists, and prefer it for writes where it does.

        Where it IS available a WRITE should default to it: surviving a closed lid
        mid-flash is the whole reason the mode exists, and a tuner who wanted the risk
        can still untick it.
        """
        if not hasattr(self, "staged_chk"):
            return

        available, why_not = self._staged_available()

        self.staged_chk.setEnabled(available)
        if not available:
            self.staged_chk.setChecked(False)
            self.staged_chk.setToolTip(why_not)
        else:
            self.staged_chk.setToolTip(
                "The head unit runs the whole job by itself and reports progress, so "
                "closing the lid or losing Wi-Fi does not stop it.")
            # Reads default to it as well now: the argument was never only about erases.
            if self._op in (None, "read", "write"):
                self.staged_chk.setChecked(True)

        self._staged_toggled()

    def _pair_head_unit(self):
        from .gui_pair import pair_head_unit

        unit = pair_head_unit(self)
        if unit is None:
            return

        self._refresh_ports()
        # Select what was just paired: the user added it in order to use it.
        for i in range(self.port_box.count()):
            data = self.port_box.itemData(i)
            if is_remote(data) and data.host == unit.host and data.port == unit.port:
                self.port_box.setCurrentIndex(i)
                break

    def _apply_fast_init_default(self):
        """Fast-init default follows the ECU (and cable): EDC15 uses 5-baud SLOW init
        on any cable; EDC16 uses fast init, and a J2534 device forces it (those
        CAN-init-only ECUs need hardware fast init, which J2534 always does)."""
        if not hasattr(self, "fast_disable_chk") or self._is_can():
            return
        if self._ecu() == "EDC15":
            self.fast_disable_chk.setChecked(True)   # slow init default (user may override)
            self.fast_disable_chk.setEnabled(True)
        elif is_j2534(self.port_box.currentData()):
            self.fast_disable_chk.setChecked(False)  # J2534 hardware fast init, required
            self.fast_disable_chk.setEnabled(False)
        else:
            self.fast_disable_chk.setChecked(False)  # EDC16 serial: fast init default
            self.fast_disable_chk.setEnabled(True)

    def _refresh_ports(self):
        # remember the selection by a stable key so a rebuilt list (device
        # objects are recreated each scan) keeps the user's last choice
        prev = getattr(self, "_last_port_key", None) or _port_key(self.port_box.currentData())
        kind = self._transport_kind()
        # The actual scan runs in _PortScanWorker, off the GUI thread -- see
        # its docstring (bug 3a). This method now only ever does fast,
        # in-process work and returns immediately, whatever the driver does.
        self._port_scan_gen = getattr(self, "_port_scan_gen", 0) + 1
        gen = self._port_scan_gen
        self.port_box.blockSignals(True)
        self.port_box.clear()
        self.port_box.addItem("Scanning for devices...", None)
        self.port_box.blockSignals(False)
        worker = _PortScanWorker(kind, gen, self)
        worker.done.connect(lambda g, devs, prev=prev: self._on_port_scan_done(g, devs, prev))
        self._port_scan_worker = worker      # keep it alive; closeEvent() detaches it
        worker.start()

    def _on_port_scan_done(self, gen, devs, prev):
        if gen != getattr(self, "_port_scan_gen", None):
            return                            # superseded by a newer scan; drop this result
        try:
            empty = ("(no J2534 device or OpenPort found)" if self._transport_kind() == "can"
                     else "(no serial ports or J2534 devices found)")
            hint = no_device_hint() if not any(d == OPENPORT_USB for d, _ in devs) else ""
            # Only a hint the user can act on (a missing driver/library) goes in
            # the list; "no OpenPort on USB" just repeats "none found".
            if hint and not devs and not hint.startswith("no OpenPort"):
                empty = empty[:-1] + " -- OpenPort: %s)" % hint
            self.port_box.blockSignals(True)
            self.port_box.clear()
            if not devs:
                self.port_box.addItem(empty, None)
            for data, label in devs:
                self.port_box.addItem(label, data)
            # Prefer the exact port QSettings remembers (set by _restore_settings(),
            # which can no longer just match it synchronously against a port_box that
            # may still say "Scanning..."); fall back to whatever was selected before
            # this particular rescan.
            want = getattr(self, "_pending_port_repr", None)
            target = -1
            if want:
                for i in range(self.port_box.count()):
                    if repr(_port_key(self.port_box.itemData(i))) == want:
                        target = i
                        break
            if target < 0 and prev is not None:
                for i in range(self.port_box.count()):
                    if _port_key(self.port_box.itemData(i)) == prev:
                        target = i
                        break
            if target >= 0:
                self.port_box.setCurrentIndex(target)
            self.port_box.blockSignals(False)
        except RuntimeError:
            return                            # the dialog was already closed/destroyed
        self._pending_port_repr = None
        self._last_port_key = _port_key(self.port_box.currentData())
        self._port_changed()

    def _remember_port(self):
        self._last_port_key = _port_key(self.port_box.currentData())

    def _port_pref_key(self, ecu=None, transport=None):
        """Settings key for the cable, per (controller, transport).

        A single flat ``flash/port`` could only remember one cable, so
        switching K-line to CAN left the serial port selected instead of
        bringing back the J2534 device last used on CAN -- and switching back
        had then overwritten the serial one.
        """
        return "port/%s/%s" % (ecu or self._ecu(), transport or self._transport_kind())

    def _want_port_for(self, ecu, transport):
        """The cable last used on this (controller, transport), or the old
        flat setting as a fallback so an upgrade keeps what was selected."""
        s = QtCore.QSettings(*branding.QSETTINGS)
        s.beginGroup("flash")
        try:
            legacy = [self._port_pref_key(old, transport)
                      for old, new in LEGACY_ECUS.items() if new == ecu]
            return (s.value(self._port_pref_key(ecu, transport))
                    or next((s.value(k) for k in legacy if s.value(k)), None)
                    or s.value("port") or None)
        finally:
            s.endGroup()

    def _speed(self):
        return self.speed_box.currentData() or "Medium"

    # ---- persisted settings (last-used = next default) ----
    def _source_mpc_image(self):
        """(mpc_image 0x81000 or None, label) -- the MPC internal-flash image from
        whatever container is loaded: .dim, .edc16, or .sgo. Only .dim needs
        checksum correction (done by the caller with the external image as key);
        .sgo/.edc16 are already final."""
        prim = getattr(self._window, "primary", None)
        if prim is None:
            return None, None
        chunks = getattr(prim, "_dim_chunks", None)
        if chunks is not None:
            try:
                from .. import dim as _dim
                return _dim.internal_flash(chunks), "dim"
            except Exception:
                return None, None
        cont = getattr(prim, "_container", None)
        if cont is not None:
            m = cont.mpc()
            return (bytes(m), "edc16") if m else (None, None)
        sc = getattr(prim, "_sgo_container", None)
        if sc is not None:
            sec = next((x for x in sc.sections
                        if x.addr == 0x404000 and x.plaintext is not None), None)
            if sec is not None:
                buf = bytearray(b"\xFF" * 0x81000)      # VMA 0x400000..0x481000
                buf[0x4000:0x4000 + len(sec.plaintext)] = sec.plaintext
                return bytes(buf), "sgo"
        # a project's tab: the internal flash kept with its set (a companion
        # or a member of the set), as saved -- already final
        ref = getattr(getattr(self._window, "current", None), "project_ref", None)
        if ref is not None:
            try:
                from ..project import companions as _co
                from . import edc16_blocks as _b
                m = _co.bytes_of(ref.doc.project, ref.set.id, "mpc",
                                 branch=ref.bid)
                m = _b.pad_mpc(m) if m is not None else None
                if m is not None and len(m) >= _b.MPC_IMAGE_SIZE:
                    return bytes(m), "project"
            except Exception:
                pass
        return None, None

    def _source_program_end(self):
        """Container-declared program-section end (exclusive), or None to let the
        write path scan the image. A .sgo section [addr 0x40000] declares the real
        extent (P475 = 0x40000+0x140000 = 0x180000)."""
        prim = getattr(self._window, "primary", None)
        sc = getattr(prim, "_sgo_container", None) if prim is not None else None
        if sc is not None:
            sec = next((x for x in sc.sections if x.addr == 0x040000), None)
            if sec is not None:
                return sec.addr + sec.declared_len
        return None

    def _block_sources(self):
        """(external_image, mpc_image) for block-availability -- side-effect free."""
        image = None
        prim = getattr(self._window, "primary", None)
        if prim is not None:
            try:
                image = _image.normalize(bytes(prim.binfile.data))
            except Exception:
                try:
                    image = bytes(prim.binfile.data)
                except Exception:
                    image = None
        try:
            mpc, _lbl = self._source_mpc_image()
        except Exception:
            mpc = None
        return image, mpc

    def _adv_open(self) -> bool:
        return bool(getattr(self, "adv_toggle", None)
                    and self.adv_toggle.isChecked())

    def _adv_visible(self, w, flag: bool):
        """Record what this advanced control's own conditions want, then apply it.

        Kept separate from ``setVisible`` because two things decide whether an
        advanced control shows: the section being expanded, and the control's own
        rule (ECU type, mode). Calling ``setVisible`` directly would let whichever
        ran last win -- expanding the section would reveal an EDC16-only button on
        an EDC15, or an ECU change would reveal a control inside a collapsed
        section.
        """
        w._adv_wanted = bool(flag)
        w.setVisible(bool(flag) and self._adv_open())
        self._adv_show_toggle()

    def _adv_show_toggle(self):
        """Advanced is always on screen (so the window keeps its shape) but
        only usable when this ECU has something in it."""
        toggle = getattr(self, "adv_toggle", None)
        if toggle is None:
            return
        any_ = any(getattr(w, "_adv_wanted", True)
                   for w in getattr(self, "_adv_children", ()))
        toggle.setVisible(True)
        toggle.setEnabled(any_)
        toggle.setToolTip(self._ADV_TIP if any_
                          else "Nothing in Advanced applies to this control unit.")
        if getattr(self, "adv_panel", None) is not None:
            show = any_ and self._adv_open()
            if show and not self.adv_panel.isVisibleTo(self) \
                    and getattr(self, "_adv_base", None) is None:
                # the panel is about to appear (an ECU change made Advanced
                # usable while it is open): the layout grows the window on its
                # next pass, so the collapsed size is taken now, before that
                self._adv_base = QtCore.QSize(self.size())
            self.adv_panel.setVisible(show)

    def _adv_sync(self):
        """Apply the expanded state AND each control's own rule."""
        on = self._adv_open()
        for w in getattr(self, "_adv_children", ()):
            w.setVisible(on and getattr(w, "_adv_wanted", True))
        if getattr(self, "adv_panel", None) is not None:
            self.adv_panel.setVisible(on)
        self._adv_show_toggle()
        if getattr(self, "adv_toggle", None) is not None:
            self.adv_toggle.setArrowType(
                QtCore.Qt.ArrowType.DownArrow if on
                else QtCore.Qt.ArrowType.RightArrow)

    _ADV_TIP = ("Diagnostics and recovery. Nothing here is needed for an ordinary "
                "read, write or backup.")

    #: QSettings key of the remembered size of the dialog (collapsed Advanced)
    _SIZE_KEY = "flash/dialog_size"

    def _init_size(self):
        """The dialog's size with Advanced collapsed: what was left last time
        when that is larger than the content needs. Everything that could
        change the content's size is reserved (see gui_dialogbase), so this is
        the same for every unit and transport."""
        self.layout().activate()
        hint = self.sizeHint()
        saved = _dlgbase.load_size(self._SIZE_KEY)
        size = hint if saved is None else QtCore.QSize(
            max(saved.width(), hint.width()), max(saved.height(), hint.height()))
        self._adv_base = None
        self.resize(size)

    def _adv_absorb(self):
        """Advanced open: a manual resize since the window was last fitted is
        the difference between the window now and the size ``_adv_refit`` gave
        it. Fold that offset into the collapsed base size, so the user's
        resize survives collapsing (and is what is remembered)."""
        base = getattr(self, "_adv_base", None)
        fitted = getattr(self, "_adv_fitted", None)
        if base is None or fitted is None:
            return
        cur = self.size()
        dw, dh = cur.width() - fitted.width(), cur.height() - fitted.height()
        if dw or dh:
            self._adv_base = QtCore.QSize(max(base.width() + dw, 1),
                                          max(base.height() + dh, 1))
            self._adv_fitted = QtCore.QSize(cur)

    def _base_size(self):
        """The size with Advanced collapsed (what is remembered)."""
        self._adv_absorb()
        return self._adv_base if getattr(self, "_adv_base", None) is not None \
            else self.size()

    def done(self, r):
        try:
            _dlgbase.save_size(self._SIZE_KEY, self._base_size())
        except Exception:                      # noqa: BLE001
            pass
        super().done(r)

    def _adv_refit(self):
        """Advanced open: the window is the base size plus the panel, in height
        only. Collapsed: exactly the base size again."""
        self._adv_absorb()                           # a manual resize while open
        base = getattr(self, "_adv_base", None)
        panel_on = self._adv_open() and self.adv_panel.isVisibleTo(self)
        if panel_on:
            if base is None:
                base = self._adv_base = QtCore.QSize(self.size())
            self.layout().activate()
            extra = self.adv_panel.sizeHint().height() + self.layout().spacing()
            self.resize(base.width(), base.height() + extra)
            self._adv_fitted = QtCore.QSize(self.size())
        elif base is not None:
            self._adv_base = None
            self._adv_fitted = None
            self.layout().invalidate()
            self.layout().activate()                # the minimum height drops first
            self.resize(base)

    def _adv_toggled(self, on: bool):
        # Boot mode changes what Read and Write DO, so it must never be armed out
        # of sight: while it is ticked the section refuses to collapse.
        if not on and (self.boot_mode.isChecked() or self.probe_chk.isChecked()
                       or self._zf_forced()):
            self.adv_toggle.setChecked(True)
            return
        if on and getattr(self, "_adv_base", None) is None:
            self._adv_base = QtCore.QSize(self.size())   # before the panel grows the window
        self._adv_sync()
        try:
            s = QtCore.QSettings(*branding.QSETTINGS)
            s.beginGroup("flash")
            s.setValue("advanced_open", on)
            s.endGroup()
        except Exception:
            pass
        self._adv_refit()

    #: what is remembered for each module on its own (Bear: the flasher
    #: opened from the ECU tab and from the TCU tab must each bring back
    #: their own connection, not whichever was used last)
    _PER_MODULE = ("transport", "speed", "can_rate", "zf6hp_transport")

    def _module_values(self):
        return {"transport": self._transport_kind(), "speed": self._speed(),
                "can_rate": self.can_rate.currentData() or 500000,
                "zf6hp_transport": self._zf_override()}

    def _save_settings(self, ecu=None):
        """Remember this module's setup under ``flash/<key>/<module>`` and
        the module as the last one used. The flat keys are written too, for
        older versions of the app."""
        ecu = ecu or self._ecu()
        vals = self._module_values()
        s = QtCore.QSettings(*branding.QSETTINGS)
        s.beginGroup("flash")
        port = repr(_port_key(self.port_box.currentData()))
        s.setValue("port", port)
        s.setValue(self._port_pref_key(ecu), port)
        s.setValue("ecu", ecu)
        s.setValue("last_ecu", ecu)
        for k, v in vals.items():
            s.setValue(k, v)
            s.setValue("%s/%s" % (k, ecu), v)
        s.setValue("_migrated", True)
        s.endGroup()
        s.sync()

    def _migrate_settings(self, s):
        """Once: a flat ``flash/*`` group from before per-module keys
        becomes that module's own settings. ``s`` is in group ``flash``."""
        if _as_bool(s.value("_migrated", False), False):
            return
        ecu = s.value("ecu")
        legacy = ecu in LEGACY_ECUS
        if legacy:
            ecu = LEGACY_ECUS[ecu]
        if ecu:
            if legacy:
                # the merged ZF6HP entry: Automatic picks UDS for a 6HP28
                s.setValue("zf6hp_transport/%s" % ecu, "auto")
            for k in self._PER_MODULE:
                v = s.value(k)
                if v is not None and s.value("%s/%s" % (k, ecu)) is None:
                    s.setValue("%s/%s" % (k, ecu), v)
            if s.value("last_ecu") is None:
                s.setValue("last_ecu", ecu)
        s.setValue("_migrated", True)

    def _module_settings(self, ecu):
        """This module's remembered setup (falling back to the last setup of
        any module for what it has none of yet)."""
        s = QtCore.QSettings(*branding.QSETTINGS)
        s.beginGroup("flash")
        out = {}
        for k in self._PER_MODULE:
            v = s.value("%s/%s" % (k, ecu))
            out[k] = v
        s.endGroup()
        return out

    def _apply_module_settings(self, ecu, transport=True):
        """Bring back ``ecu``'s own transport (unless ``transport`` is False),
        speed, CAN rate and cable (when it has any remembered)."""
        vals = self._module_settings(ecu)
        self._restoring = True
        try:
            if ecu == "ZF6HP":
                zf = vals.get("zf6hp_transport") or "auto"
                j = self.zf_transport_box.findData(zf)
                self.zf_transport_box.setCurrentIndex(j if j >= 0 else 0)
            tr = vals.get("transport") if transport else None
            if tr in ("kline", "can") and not (
                    (ecu in KLINE_ONLY_ECUS and tr == "can")
                    or (ecu in CAN_ONLY_ECUS and tr == "kline")):
                i = self.transport_box.findData(tr)
                if i >= 0 and i != self.transport_box.currentIndex():
                    self.transport_box.setCurrentIndex(i)
            want = self._want_port_for(ecu, self._transport_kind())
            if want:
                self._pending_port_repr = want
                self._apply_pending_port()
            cr = vals.get("can_rate")
            if cr not in (None, ""):
                j = self.can_rate.findData(int(cr))
                if j >= 0:
                    self.can_rate.setCurrentIndex(j)
            sp = vals.get("speed")
            if sp:
                j = self.speed_box.findData(sp)
                if j >= 0:
                    self.speed_box.setCurrentIndex(j)
        finally:
            self._restoring = False

    def _apply_pending_port(self):
        """Select the remembered cable now if the device list has it (the
        scan may still be running: then _on_port_scan_done applies it)."""
        want = getattr(self, "_pending_port_repr", None)
        if not want:
            return
        for i in range(self.port_box.count()):
            if repr(_port_key(self.port_box.itemData(i))) == want:
                self.port_box.setCurrentIndex(i)
                return

    def _ecu_picked(self, *_a):
        """The module was changed (by hand, or for the open file): keep the
        one left, then bring back the new one's own setup."""
        prev = getattr(self, "_shown_ecu", None)
        new = self._ecu()
        if getattr(self, "_restoring", False) or prev == new:
            self._shown_ecu = new
            return
        if prev:
            try:
                self._save_settings(prev)
            except Exception:
                pass
        self._shown_ecu = new
        # Force full write is a one-off for the write in front of you, never a
        # setting: it is not remembered, and a tick made for one control unit
        # does not carry over to the next one picked.
        self.force_full.setChecked(False)
        self._apply_module_settings(
            new, transport=not getattr(self, "_ecu_from_transport", False))

    def _restore_settings(self):
        s = QtCore.QSettings(*branding.QSETTINGS)
        s.beginGroup("flash")
        self._migrate_settings(s)
        ecu = s.value("last_ecu") or s.value("ecu")
        portrepr = s.value("port")
        adv_open = _as_bool(s.value("advanced_open", False), False)
        s.endGroup()
        s.sync()
        if ecu in LEGACY_ECUS:
            # e.g. "ZF6HP-UDS" -> "ZF6HP": the 6HP28 entry was merged into
            # ZF6HP, and Automatic picks UDS for a 6HP28 by itself.
            ecu = LEGACY_ECUS[ecu]
        # an open file's platform decides the module; the last one used
        # applies when there is no file to go by
        from .standalone import ecu_for_platform
        prim = getattr(self._window, "primary", None)
        file_ecu = ecu_for_platform(getattr(prim, "platform", None)) \
            if prim is not None else None
        if file_ecu and self.ecu_box.findText(file_ecu) >= 0:
            ecu = file_ecu
        if adv_open and hasattr(self, "adv_toggle"):
            self.adv_toggle.setChecked(True)
            self._adv_sync()
        # (Checked against the box's own items, not a hardcoded tuple: a
        # module added later is restored too -- the 2026-09-21 bug.)
        self._restoring = True
        try:
            if ecu and self.ecu_box.findText(ecu) >= 0:
                self.ecu_box.setCurrentText(ecu)        # -> _ecu_changed
        finally:
            self._restoring = False
        self._shown_ecu = self._ecu()
        # The device scan runs off the GUI thread (_PortScanWorker) and can
        # land after this returns: remember which port to reselect and let
        # _on_port_scan_done() apply it once a real scan result arrives.
        self._pending_port_repr = portrepr or None
        self._apply_module_settings(self._ecu())
        # fast/slow init is derived from the ECU (EDC15 -> slow), not restored

    def closeEvent(self, ev):
        try:
            self._save_settings()
        except Exception:
            pass
        worker = getattr(self, "_port_scan_worker", None)
        if worker is not None and worker.isRunning():
            # Do NOT wait() here -- that would reintroduce exactly the kind of
            # GUI-thread block this class exists to avoid, if the scan is the
            # one that's actually wedged. Just stop caring about its result;
            # the generation check in _on_port_scan_done() (and its RuntimeError
            # guard, once this dialog's widgets are gone) makes a late result
            # harmless if the driver call underneath it ever does return.
            try:
                worker.done.disconnect()
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(ev)

    @staticmethod
    def _fmt_elapsed(secs):
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))

    def _tick_timer(self):
        if self._t0 is not None:
            self.timer_lbl.setText(self._fmt_elapsed(time.monotonic() - self._t0))

    def _start_timer(self):
        self._t0 = time.monotonic()
        self.timer_lbl.setText("0:00")
        self._op_timer.start()

    def _stop_timer(self):
        if self._t0 is not None:
            self._last_elapsed = time.monotonic() - self._t0
            self._tick_timer()                       # freeze on the final elapsed time
        self._op_timer.stop()
        self._t0 = None

    def _busy(self, on):
        for w in (self.read_btn, self.write_btn,
                  self.ee_read_btn, self.ee_write_btn, self.irom_btn,
                  self.backup_btn, self.restore_btn,
                  self.port_box, self.speed_box, self.ecu_box,
                  self.force_full, self.boot_mode, self.close_btn, self.fast_disable_chk,
                  self.transport_box, self.can_rate,
                  self.prog_arm_btn, self.prog_cksumprobe_btn,
                  self.prog_seedprobe_btn, self.prog_bootprobe_btn,
                  self.prog_ramread_btn, self.prog_readfull_btn,
                  self.zf_transport_box, self.uds_cal_only_chk, self.vdo_verify_chk):
            w.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        if on:
            self._start_timer()
        else:
            self._stop_timer()
            self._boot_toggled()
            self._transport_changed()

    # ---- operations ----
    def _start(self, op):
        port = self.port_box.currentData()
        can = self._is_can()
        if port is None:
            QtWidgets.QMessageBox.warning(
                self, "Flashing",
                "Select a J2534 device." if can else "Select a K-line cable or J2534 device.")
            return
        if can and op not in ("read", "write") + _gui16.CAN_OPS:
            QtWidgets.QMessageBox.warning(
                self, "Flashing", "Over CAN only flash read/write are available; "
                "EEPROM and EDC15 need K-line.")
            return
        if self._ecu() == "Cluster" and op != "read":
            QtWidgets.QMessageBox.warning(
                self, "Flashing",
                "The Cluster module is read-only in this version "
                "(write is not implemented yet).")
            return
        if op in ("write", "restore", "write_program", "ee_write"):
            # a project tab with unsaved edits may be saved first, so the
            # version written is in its history (and marked as flashed)
            hook = getattr(self._window, "before_flash_write", None)
            if hook is not None and not hook():
                return
        if self._ecu() == VDO_ECU:
            return self._start_vdo(op, port, can)
        # ZF6HP: a read takes the Advanced choice as it is ("auto" = TP 2.0, then
        # UDS if nothing answers); a write is resolved here, from the override or
        # the open file, so the confirmation can say which transport it uses.
        zf_choice = zf_path = None
        if self._ecu() == "ZF6HP":
            if op == "write":
                zf_choice = self._zf_write_choice()
                zf_path = zf_choice.transport if zf_choice is not None else "tp20"
            else:
                zf_path = self._zf_override()
        clim_hvac = self._ecu() == "Climatronic"
        image = None
        edc16_opts = None
        # --- Climatronic write / test-write: flash the image open in the app AS
        # IT IS NOW (unsaved edits included, like every other write in this
        # dialog); only prompt for a file if no Climatronic image is open ---
        if clim_hvac and op == "read":
            QtWidgets.QMessageBox.warning(
                self, "Flashing", "The Climatronic can't be read over OBD; it can "
                "only be written.")
            return
        if clim_hvac and op == "write":
            from .. import recent as _recent
            from .hvac_image import HvacImage, HvacImageError
            verb = "flash"
            image = self._loaded_hvac_image()
            if image is not None:
                src = "%s (as open in %s, including unsaved changes)" % (
                    image.name or "the open file", branding.APP_NAME)
                _debug_log("Climatronic %s: using the open image %s" % (op, image.name))
            else:
                path, _f = QtWidgets.QFileDialog.getOpenFileName(
                    self, "Choose Climatronic image to %s" % verb, _recent.get_last_dir(),
                    "HVAC flash images (*.sgo *.hvac);;All files (*)")
                if not path:
                    return
                _recent.set_last_dir(os.path.dirname(path))
                try:
                    image = HvacImage.load(path, name=os.path.basename(path))
                except (HvacImageError, OSError, ValueError) as ex:
                    QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
                    return
                src = os.path.basename(path)
            if op == "write":
                try:
                    self._hvac_checksum_or_confirm(image)   # as Save would
                except _WriteCancelled:
                    return
                except Exception as ex:
                    QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
                    return
                if QtWidgets.QMessageBox.question(
                        self, "Write Climatronic",
                        "This will ERASE and REPROGRAM the A/C module flash from:\n\n%s\n\n"
                        "An interrupted or wrong write can brick the module. If a write "
                        "aborts partway, write the file again: the write starts over "
                        "from the first block.\n\nProceed?" % src) \
                        != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
        elif op == "write" and self._ecu() == "ZF6HP" and zf_path == "uds":
            image = self._confirm_zf6hp_uds_write(zf_choice)
            if image is None:
                return
        elif op == "write":
            try:
                image = self._prepare_write_image(zf_transport=zf_path)
            except _WriteCancelled:
                return
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
                return
            if self._ecu() == "EDC15" and self.boot_mode.isChecked():
                what = "the WHOLE flash chip (boot mode: full erase, then every block)"
            elif self._ecu() == "ZF6HP":
                what = "the TCU's calibration flash over TP 2.0 (6HP19 / 6HP26)"
            else:
                what = ("the changed flash sectors" if self._ecu() == "EDC15"
                        else "the ECU's calibration flash")
            if QtWidgets.QMessageBox.question(
                    self, "Write to ECU",
                    "This will ERASE and REPROGRAM %s.\n"
                    "An interrupted or wrong write can brick the ECU.%s\n\nProceed?"
                    % (what, self._zf_guess_note(zf_choice))) \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        elif op in _gui16.BLOCK_OPS or op in _gui16.RESEARCH_OPS:
            edc16_opts = _gui16.prepare(self, op)
            if edc16_opts is None:
                return
            image = edc16_opts.pop("image", None)
        elif op == "ee_write":
            image = self._held_eeprom()
            source = "the loaded backup's EEPROM" if image is not None else None
            if image is None:
                image = self._pick_eeprom_file()
            if image is None:
                return
            info = _edc15.eeprom_info(image)
            if QtWidgets.QMessageBox.question(
                    self, "Write EEPROM",
                    "Write %s to the ECU?\n\n%s\n\n"
                    "Only bytes that differ from the ECU's current EEPROM are written, "
                    "then the EEPROM is read back and verified."
                    % (source or "this 512-byte EEPROM image", _info_text(info))) \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        elif op == "restore":
            try:
                flash_img = self._prepare_write_image()
            except _WriteCancelled:
                return
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
                return
            ee = self._window_eeprom()
            if ee is None:
                QtWidgets.QMessageBox.warning(self, "Flashing", "No EEPROM is loaded.")
                return
            image = (flash_img, bytes(ee))
            if QtWidgets.QMessageBox.question(
                    self, "Write flash + EEPROM",
                    "Write the changed flash sectors, then the EEPROM, from the loaded "
                    "backup?\nAn interrupted write can brick the ECU.\n\nProceed?") \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._op = op
        self._save_settings()
        self.bar.setValue(0)
        # (The flash trace -- Tools > Options > Flasher -- is the worker's: it
        # reads the options itself, so every ECU and cable is covered.)
        self._busy(True)
        # K-line fast init: always on for a J2534 device (hardware); the checkbox for
        # a serial cable (break pulse, default on).  CAN has no fast init.
        fast_init = (self._ecu() != "EDC15") and (not can) and (
            is_j2534(port) or not self.fast_disable_chk.isChecked())
        zf_uds = self._ecu() == "ZF6HP" and zf_path == "uds"
        self._worker = _Worker(op, self._ecu(), port, self._speed(), image=image,
                               # Force full write is TP 2.0's; Calibration only UDS's
                               force_full=self.force_full.isChecked() and not zf_uds,
                               hvac_selective=self.hvac_selective.isChecked(),
                               boot_mode=self.boot_mode.isChecked(),
                               transport=("can" if can else "kline"),
                               can_dev=port if can else None,
                               can_rate=self.can_rate.currentData() or 500000,
                               fast_init=fast_init,
                               staged=(self.staged_chk.isChecked()
                                       and self.staged_chk.isEnabled()),
                               edc16=edc16_opts,
                               uds_cal_only=zf_uds and self.uds_cal_only_chk.isChecked(),
                               zf_transport=zf_path or "auto",
                               # ZF6HP does not offer the probe checkbox (2026-09-22),
                               # so probe is always False for it --
                               # Cluster/ABS keep the diagnostic.
                               probe=(self._ecu() in ("Cluster", "ABS")
                                      and self.probe_chk.isVisible()
                                      and self.probe_chk.isChecked()))
        self._worker.stage.connect(self._show_stage)
        self._worker.percent.connect(self.bar.setValue)
        self._worker.finished_ok.connect(lambda r: self._done(op, r))
        self._worker.failed.connect(self._error)
        self._worker.start()

    def _show_stage(self, text):
        """A step on the status line. The ECU's "busy" notices are left off
        it: the step it is busy with (an erase, say) stays on screen."""
        from .tp20_transport import BUSY
        if not str(text).startswith(BUSY):
            self.stage.setText(text)

    def _loaded_hvac_image(self):
        """The Climatronic image open in the app AS IT IS NOW -- a copy of the
        session's HvacImage (loader, sections, stock baseline) with the editor's
        current bytes synced in, exactly what Save would write -- or None when
        the open file is not a Climatronic image (the caller then prompts).

        For a Climatronic .sgo / .hvac the session assembles binfile.data at the
        firmware's own flash base (see TunerSession: ``self._hvac``), so it IS
        the HVAC image; the copy keeps the session's own object untouched."""
        import copy
        prim = getattr(self._window, "primary", None)
        him = getattr(prim, "_hvac", None) if prim is not None else None
        if him is None:
            return None
        img = copy.copy(him)
        img.image = bytearray(him.image)
        data = bytes(prim.binfile.data)
        n = min(len(img.image), len(data))
        img.image[:n] = data[:n]
        return img

    def _hvac_checksum_or_confirm(self, himg):
        """Correct ``himg``'s internal flash checksum in place, as Save does --
        or, when it cannot be corrected (the 4F/4L HVAC module is missing, or
        it has no checksum mapped for this firmware), ask before it is written
        as it is. Raises :class:`_WriteCancelled` on a no."""
        if _registry.for_platform("hvac") is None:
            self._checksum_or_confirm("hvac")        # module missing: asks
            return
        cs = _registry.checksum("hvac", image=himg)
        if cs is not None and cs.is_supported(bytes(himg.image)):
            himg.image[:] = cs.correct(bytes(himg.image))
            return
        if QtWidgets.QMessageBox.question(
                self, "Checksum cannot be corrected",
                "No internal flash checksum is mapped for this Climatronic firmware, "
                "so its checksum can be neither checked nor corrected: the image is "
                "written exactly as it is. An image edited since its checksum was "
                "last correct can be rejected by the module.\n\nWrite it anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            raise _WriteCancelled()

    @staticmethod
    def _zf_guess_note(choice):
        """For a ZF6HP write confirmation: when the transport was only guessed
        (no structural identity in the file), say which and why, so the user
        can stop and force the right one in Advanced. Empty otherwise."""
        if choice is None or choice.confidence != "low":
            return ""
        name = "UDS (6HP28)" if choice.is_uds else "TP 2.0 (6HP19 / 6HP26)"
        return ("\n\nThe file does not say which ZF 6HP it is, so %s was guessed "
                "(%s). If that is wrong, answer No and choose the transport under "
                "Advanced." % (name, choice.reason))

    def _confirm_zf6hp_uds_write(self, choice):
        """ZF6HP over UDS (6HP28): the image to write, after the user confirmed
        what will be written -- or None (cancelled, or the file can't be one)."""
        try:
            image = self._prepare_write_image(zf_transport="uds")
        except _WriteCancelled:
            return None
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Flashing", str(ex))
            return None
        try:
            from . import zf6hp_uds as _uds
            _pl = _uds.classify_image(
                bytes(image), force_cal_only=self.uds_cal_only_chk.isChecked())
            _kind = "calibration only" if _pl.cal_only else "program + calibration"
            _wr = ", ".join(b.spec.name for b in _pl.write_set) or "(nothing)"
            _warn = ("\n\nWARNING:\n  " + "\n  ".join(_pl.warnings)) if _pl.warnings else ""
        except Exception as ex:
            QtWidgets.QMessageBox.critical(
                self, "Flashing",
                "Couldn't read the loaded file as a 6HP28 flash image: %s" % ex)
            return None
        # Zf6hpUdsWriter.write() has no pre-write skip (the TCU's 0x0202
        # routine is a post-download check only), so every candidate block is
        # erased and rewritten; Force full write is not offered over UDS.
        _ff = ("\n\nEvery listed block is erased and rewritten, then checked by "
               "the TCU. %s" % ("Its checksums were corrected and checked first."
                                if getattr(self, "_uds_corrected", False) else
                                "The file is sent as it is: the ZF 6HP module cannot "
                                "read its checksum layout, so the checksum is neither "
                                "checked nor corrected."))
        if QtWidgets.QMessageBox.question(
                self, "6HP28 write",
                "DESTRUCTIVE: writes this image to the TCU over OBD/UDS.\n\n"
                "Detected: %s (candidate blocks: %s).%s%s%s\n\n"
                "Do this with the engine OFF and a stable 12 V supply. An interrupted "
                "program write can leave the TCU needing bench recovery.\n\n"
                "Proceed with the write?"
                % (_kind, _wr, _ff, _warn, self._zf_guess_note(choice)),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return None
        return image

    def _prepare_write_image(self, zf_transport=None):
        """The open file as it will be written, checksum-corrected where the
        platform allows. ``zf_transport`` ("tp20" / "uds") picks the ZF6HP
        path; None resolves it like a write would (_zf_write_choice)."""
        prim = self._window.primary
        if prim is None:
            raise RuntimeError("Choose a file to write first (Open file...)." if self._standalone
                               else "Open or read a file first, then write it.")
        raw = bytes(prim.binfile.data)
        if self._ecu() == "EDC15":
            if _platforms.edc15.is_v3_sets_only(raw):
                raise RuntimeError(
                    "This MSA15 / EDC15V file holds only its calibration sets, "
                    "with no program code, so it cannot be written to an ECU.")
            if _platforms.edc15.is_v3(raw):
                raise RuntimeError(
                    "This is an MSA15 / EDC15V image (256 KB). The flasher writes "
                    "EDC15VM+ / EDC15P ECUs (512 KB) only; write an MSA15 / EDC15V "
                    "file with your own programmer. Save it first: saving corrects "
                    "its checksums.")
            if len(raw) == _edc15.LAYOUT_1M.size:
                raise RuntimeError(_edc15.WRITE_1M_UNSUPPORTED + " Reading a "
                                   "1 MB EDC15 works; writing one does not yet.")
            if len(raw) not in (_edc15.LAYOUT_512.size, _edc15.LAYOUT_1M.size):
                raise RuntimeError(
                    "An EDC15 flash image must be 512 KB (0x80000) or 1 MB (0x100000 "
                    "bytes); this file is %d bytes. If it's an EDC16 file, pick ECU: "
                    "EDC16." % len(raw))
            if not getattr(prim, "is_edc15", False) and not _platforms.edc15.detect(raw):
                raise RuntimeError(
                    "The current file isn't recognized as an EDC15 (V4.1) image.")
            cs = self._checksum_or_confirm("edc15")
            if cs is not None and not cs.is_supported(raw):
                # (the first EDC15P software's "P2" sets: no checksum routine
                # the app reads -- written as edited, the ECU would not start)
                raise RuntimeError(
                    "The checksums of this EDC15 image are not ones the app can "
                    "correct (%s), so it is not written: an image with wrong "
                    "checksums does not start." % (
                        "the first EDC15P software, \"P2\" datasets"
                        if _platforms.edc15.datasets_p2(raw) else "unknown checksum routine"))
            if cs is not None and cs.is_supported(raw):
                # Refusing is the point: an image whose real checksum ranges
                # cannot be read must not be flashed with guessed ones.
                raw = self._correct_and_check("EDC15", cs, raw)
            return raw
        if self._ecu() == "ZF6HP" and zf_transport is None:
            _c = self._zf_write_choice()
            zf_transport = _c.transport if _c is not None else "tp20"
        if self._ecu() == "ZF6HP" and zf_transport == "uds":
            # 6HP28 OBD/UDS write: a 2 MB full image or a 512 KB calibration
            # dump. Its checksums are corrected (and checked) wherever the ZF 6HP
            # module can read their layout; where it cannot, the image goes as
            # it is and classify_image's plan warns about the checksum.
            self._uds_corrected = False
            cs = self._checksum_or_confirm("zf6hp")
            if cs is not None and cs.is_supported(raw):
                raw = self._correct_and_check("ZF 6HP", cs, raw)
                self._uds_corrected = True
            return raw
        if self._ecu() == "ZF6HP":
            # Bug fix (2026-09-21): this used to fall through to the EDC16-only
            # branch below, which slices the last 512 KB and checks it against
            # the EDC16 0xCAFECADE checksum layout -- guaranteed to fail on a
            # ZF6HP image (wrong size class, wrong checksum family entirely),
            # even for a file _zf6hp.Zf6hpFlash.read_image() had *just*
            # produced. ZF6HP has its own full-image size (IMAGE_SIZE, 1 MiB --
            # see zf6hp.py's read_image()/write_image()) and its own
            # calibration checksum (CRC-16/CCITT-FALSE over a data-block
            # region located by descriptor, not the EDC16 additive sum -- see
            # ovtsuite/zf6hp/checksum6hp.py).
            if len(raw) < _zf6hp.IMAGE_SIZE:
                raise RuntimeError(
                    "A ZF 6HP flash image must be at least %d bytes (0x%X); this "
                    "file is %d bytes (0x%X). If it's an EDC16/EDC15 file, pick "
                    "the matching ECU instead."
                    % (_zf6hp.IMAGE_SIZE, _zf6hp.IMAGE_SIZE, len(raw), len(raw)))
            # Checksum correction: some real ZF6HP layouts (e.g. certain 6HP28
            # code-block-only extracts) aren't recognized by checksum6hp yet
            # (see project notes / ZF6HP_CHECKSUM_FINDINGS.md). Refusing outright
            # would block a legitimate write the user explicitly asked for by
            # selecting ZF6HP -- an unrecognized layout here is not evidence of
            # the wrong platform -- but writing it uncorrected without a word was
            # worse, so the user is asked (default No) and told why.
            # Honour the ZF 6HP program-area option (Options > Projects and saving; off by
            # default): when on, also rewrite the program/boot-area descriptor
            # self-CRCs, not just calibration. Offsets aren't needed here -- a
            # body-intact edit is auto-detected from the assembled image.
            _pa = bool(getattr(getattr(self._window, "primary", None),
                               "correct_program_area", False))
            cs = self._checksum_or_confirm("zf6hp")
            if cs is not None and not cs.is_supported(raw):
                # The calibration checksum's layout (its DAC/DAR descriptor)
                # cannot be read from this image, so its checksum can be neither
                # verified nor corrected. Never write that silently: ask, like
                # _checksum_or_confirm does when the module is missing.
                _desc = bool(_pa and cs.find_descriptors(raw))
                if QtWidgets.QMessageBox.question(
                        self, "Checksum cannot be corrected",
                        "The ZF 6HP calibration checksum layout cannot be read from "
                        "this image, so its calibration checksum can be neither "
                        "checked nor corrected: the calibration is written exactly as "
                        "it is.%s\n\nAn image edited since its checksum was last "
                        "correct can be rejected by the TCU, or leave it unable to "
                        "start until a correct image is written.\n\nWrite it anyway?"
                        % (" (The program-area checksums are still corrected.)"
                           if _desc else ""),
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                        QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                    raise _WriteCancelled()
                if _desc:
                    raw = cs.correct(raw, program_area=_pa)
            elif cs is not None:
                raw = self._correct_and_check("ZF 6HP", cs, raw, {"program_area": _pa})
            return raw
        img = bytes(_image.normalize(raw))
        cs = self._checksum_or_confirm("edc16")
        if cs is not None:
            # with the file's own options, as Save corrects: the KWP2000 content
            # digest included. Corrected without it (as this once did, and an
            # error here was swallowed), the block checksums were right but the
            # digest stale, and the ECU refused every written block
            # (Reference/Logs/EDC16Checksumfailwritefail.txt: 33 C5 -> 7F 33 22).
            img = self._correct_and_check("EDC16", cs, img)
        return bytes(img)

    def _checksum_opts(self, extra=None):
        """The checksum options the open file is saved with (EDC16 content
        digest, ZF 6HP program area), so a write corrects exactly as Save."""
        prim = getattr(self._window, "primary", None)
        fn = getattr(prim, "_checksum_opts", None)
        opts = {"digest": True}
        if callable(fn):
            try:
                opts = dict(fn())
            except Exception:
                pass
        opts.update(extra or {})
        opts["digest"] = True       # never optional: the ECU refuses a stale one
        return opts

    def _correct_and_check(self, name, cs, raw, extra=None):
        """``raw`` with its checksums corrected by ``cs`` -- before anything
        connects to the unit, with the options Save uses. Raises (the write
        does not start) when the correction fails, or when correcting the
        result again still changes it (the correction does not settle).

        A verify report is not the test: an edited EDC16 file keeps an RSA
        signature that only Bosch can make (the ECU takes the digest the
        correction writes instead), so its report never reads "all valid"
        though the write is accepted."""
        opts = self._checksum_opts(extra)
        raw = bytes(raw)
        if _memo.settled(cs, raw, opts):
            # already found right -- by the editor's background check, a save or
            # an earlier write of this very image: summing 2 MB twice more on
            # the window's thread is all that Write would add
            return raw
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.stage.setText("Checking the %s checksums..." % name)
            self.stage.repaint()
            fixed = bytes(cs.correct(raw, **opts))
            # an image the correction leaves as it is has settled; only a
            # changed one needs the second pass to show the result is stable
            again = fixed if fixed == raw else bytes(cs.correct(fixed, **opts))
        except Exception as ex:                       # noqa: BLE001 - said, not swallowed
            raise RuntimeError(
                "The %s checksums of this image could not be corrected (%s), so it "
                "is not written: the unit would reject it, or not start." % (name, ex))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if again != fixed:
            raise RuntimeError(
                "The %s checksums of this image do not come out the same when "
                "corrected twice, so it is not written: the correction cannot be "
                "trusted for this file." % name)
        _memo.mark(cs, fixed, opts)
        return fixed

    def _checksum_or_confirm(self, pid):
        """The checksum provider for platform ``pid`` -- or, when its module
        is not installed, ask before the image is written uncorrected.
        Returns None after a yes; raises :class:`_WriteCancelled` on a no.

        Reading and writing never need a module (the flasher supports every
        platform natively); only correcting the checksum does."""
        cs = _registry.checksum(pid)
        if cs is not None:
            return cs
        name = _platforms.platform(pid).name
        if QtWidgets.QMessageBox.question(
                self, "Checksum will not be corrected",
                "%s\n\nThe %s checksum of this image will NOT be corrected: "
                "it is written exactly as it is. An image edited since its checksum "
                "was last corrected can be rejected by the module, or leave it in "
                "boot mode until a correct image is written.\n\nWrite it anyway?"
                % (_registry.missing_text(pid, "checksum correction"), name),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            raise _WriteCancelled()
        return None

    def _held_eeprom(self):
        """EEPROM image already in hand: the editor's image, else the loaded backup's."""
        if self._eeprom_image is not None:
            return bytes(self._eeprom_image)
        return self._window_eeprom()

    def _window_eeprom(self):
        """The EEPROM that travels with the open file: a full backup's (held
        beside the flash), else the one a native .edc15 container carries."""
        ee = getattr(self._window, "_eeprom", None)
        if ee is None:
            # a tab that IS an EEPROM image (opened on its own): that is the
            # EEPROM Write EEPROM sends
            is_ee = getattr(self._window, "is_eeprom_image", None)
            prim = getattr(self._window, "primary", None)
            try:
                if callable(is_ee) and is_ee() and prim is not None \
                        and len(prim.binfile.data) == _edc15.EEPROM_SIZE:
                    ee = bytes(prim.binfile.data)
            except Exception:
                ee = None
        if ee is None:
            fn = getattr(self._window, "_container_eeprom", None)
            try:
                ee = fn() if callable(fn) else None
            except Exception:
                ee = None
            if ee is not None and len(ee) != _edc15.EEPROM_SIZE:
                ee = None            # an EDC16 container's EEPROM is not this
        return bytes(ee) if ee is not None else None

    def _pick_eeprom_file(self):
        from .. import recent as _recent
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "EEPROM image to write (512 bytes)", _recent.get_last_dir("eeprom"),
            "EEPROM images (*.bin *.eep *.rom);;All files (*)")
        if not path:
            return None
        _recent.set_last_dir(os.path.dirname(path), "eeprom")
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) != _edc15.EEPROM_SIZE:
            QtWidgets.QMessageBox.critical(
                self, "Write EEPROM",
                "%s is %d bytes; an EDC15 EEPROM image is exactly 512 bytes."
                % (os.path.basename(path), len(data)))
            return None
        return data

    def _done_zf_uds_read(self, result):
        """A ZF6HP read that went over UDS (6HP28): into a project, as a full
        2 MB flash image with the calibration at its flash address."""
        self.stage.setText("Calibration read over UDS complete (nothing written).")
        cal = result.get("cal", b"") if isinstance(result, dict) else b""
        base = result.get("base", 0x180000) if isinstance(result, dict) else 0x180000
        from .. import recent as _recent
        import datetime as _dt
        # Into a project, like every read, and ALWAYS as a full 2 MB flash
        # image, so the calibration sits at its real flash address -- in the
        # editor, in an exported .bin and in other tools (WinOLS etc.), which
        # would otherwise treat a bare 512 KB dump as starting at 0. The TCU
        # exposes only the calibration window over OBD, so the program/boot
        # area below it is 0xFF (NOR-erased), which classify_image reads as
        # blank: re-flashing the image still writes the calibration only.
        from .. import container as _cont
        from . import zf6hp_uds as _uds2
        img = bytearray(b"\xFF" * max(_uds2.IMAGE_SIZE, base + len(cal)))
        img[base:base + len(cal)] = cal
        c = _cont.Container(fmt=_cont.format_for_kind("zf6hp"))
        c.add(_cont.BLOCK_EXTERNAL, bytes(img))
        c.set_meta({"source": "obd_uds_read", "platform": "zf6hp",
                    "variant": "6hp28", "region": "calibration",
                    "flash_base": base, "padded": True})
        parent = self.parentWidget()

        def _open(win, blob, name):
            try:
                win.load_from_image(blob, name)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(parent, "6HP28 read", str(ex))
        self._hand_over(_open, self._window, c.to_bytes(),
                        "6hp28-cal-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

    def _hand_over(self, fn, *args):
        """Close the flasher first, then open the finished read: the prompt
        that follows (add to a project / save as) must not sit behind a
        flasher window still on screen (macOS keeps the older window in
        front). The load runs on the next turn of the event loop, once the
        window has gone; ``fn`` and ``args`` are captured now, so nothing
        depends on this dialog afterwards."""
        parent = self.parentWidget()
        self.accept()

        def _run():
            try:
                fn(*args)
            except Exception as ex:             # noqa: BLE001
                # a slot exception would only reach the log: tell the user,
                # the way every other flasher error is told
                try:
                    QtWidgets.QMessageBox.critical(
                        parent, "Flashing", "Opening the finished read failed: %s" % ex)
                except Exception:               # noqa: BLE001
                    pass
        QtCore.QTimer.singleShot(0, _run)

    def _flashed(self, what):
        """A write succeeded: the source tab's version is marked as flashed
        in its project (the standalone flasher marks the .ovt it opened)."""
        hook = getattr(self._window, "flash_written", None)
        if hook is not None:
            try:
                hook("%s %s" % (self._ecu(), what))
            except Exception:
                pass

    def _done_zf_uds_write(self, result):
        """A ZF6HP write that went over UDS (6HP28): report what was written."""
        report = (result.get("report", "") if isinstance(result, dict) else str(result))
        written = result.get("written", []) if isinstance(result, dict) else []
        if written:
            self._flashed("UDS write")
        self.stage.setText("Write over UDS complete -- %s."
                           % (", ".join(written) if written else "nothing written"))
        QtWidgets.QMessageBox.information(
            self, "6HP28 write", report or "(nothing written)")

    def _cancel(self):
        if self._worker:
            self.stage.setText("Stopping...")
            self._worker.request_stop()

    def _done(self, op, result):
        self._busy(False)
        self.bar.setValue(100)
        if isinstance(result, dict) and result.get("_vdo"):
            return self._done_vdo(op, result)
        if isinstance(result, dict) and result.get("_probe"):
            ids = result.get("unique_ids", {})
            id_lines = "\n".join("  %s  x%d  first=%s" % (k, v[0], v[1])
                                 for k, v in ids.items()) or "  (none)"
            replied = result.get("setup_replied")
            verdict = ("Setup REPLY on %s: %s — addressing OK; the normal read's "
                       "pre-setup id filter was hiding it." % (result["setup_reply_id"],
                                                               result.get("reply_payload"))
                       if replied else
                       ("No frames at all — wrong bus or wrong baud (try 100000)."
                        if result.get("frames_total", 0) == 0 else
                        "Bus alive but nothing on %s — module not on this bus, or the "
                        "gateway isn't routing TP2.0 to dest 0x%02X (try 100000 / comfort CAN)."
                        % (result["setup_reply_id"], result.get("dest", 0))))
            self.stage.setText("Probe done — %d frames." % result.get("frames_total", 0))
            QtWidgets.QMessageBox.information(
                self, "%s bus probe" % result.get("ecu", ""),
                "Sent 0x200#%s at %d bit/s.\n\nFrames: %d across %d id(s):\n%s\n\n%s"
                % (result.get("setup", "?"), result.get("baud", 0),
                   result.get("frames_total", 0), len(ids), id_lines, verdict))
            self.accept()
            return
        if isinstance(result, dict) and result.get("_uds_read"):
            return self._done_zf_uds_read(result)
        if isinstance(result, dict) and result.get("_uds_write"):
            return self._done_zf_uds_write(result)
        if op == "irom_read" and result is not None:
            from . import irom15
            # Pass the flash image when the dialog has one: the image states the
            # checksum its boot path expects of the ROM, which turns a structural
            # guess into a decision.
            # `_image` is the write payload: bytes for a plain write, or a
            # (flash, eeprom) pair for a restore. Either way it is only set for a
            # write; a read-only session has none, and then the check falls back
            # to its structural tests.
            img = self._image
            if isinstance(img, (tuple, list)) and img:
                img = img[0]
            if not (isinstance(img, (bytes, bytearray)) and len(img) >= 0x80000):
                img = None
            rep = irom15.check(result, image=bytes(img) if img else None)
            self.stage.setText("Internal ROM read — %s."
                               % ("looks sound" if rep.ok else "SUSPECT, see the report"))
            from .. import recent as _recent
            fname = "C167-IROM-%04X-%s.bin" % (
                rep.version_sum, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            keep = getattr(self._window, "keep_attachment", None)
            path = None
            if keep is not None and keep(fname, bytes(result),
                                         "the C167 internal-ROM dump"):
                path = "the project's attachments (%s)" % fname
            else:
                path, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, "Save C167 internal-ROM dump",
                    os.path.join(_recent.get_last_dir(), fname),
                    "ROM dumps (*.bin);;All files (*)")
                if path:
                    with open(path, "wb") as fh:
                        fh.write(result)
                    _recent.set_last_dir(os.path.dirname(path))
            body = rep.describe()
            if not rep.ok:
                # The C167's failure mode is silent, so this has to be said plainly
                # rather than left to the numbers.
                body += ("\n\nRe-read before using this dump. A wrong bus/register "
                         "configuration returns every other byte doubled instead of "
                         "failing, so a bad read looks like a good one.")
            if path:
                body += "\n\nSaved %d bytes to %s." % (
                    len(result), path if path.startswith("the project")
                    else os.path.basename(path))
            (QtWidgets.QMessageBox.information if rep.ok
             else QtWidgets.QMessageBox.warning)(self, "C167 internal ROM", body)
            self.accept()
        elif op == "read" and result is not None and self._ecu() == "Cluster":
            # like every read: into a project (the standalone flasher saves an
            # .ovt, or the raw dump)
            ecu = self._ecu()
            self.stage.setText("%s read complete." % ecu)
            name = "%s-%s" % (ecu, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            self._hand_over(self._window.load_from_image, bytes(result), name)
        elif op == "read" and result is not None:
            how = " over TP 2.0" if self._ecu() == "ZF6HP" else ""
            self.stage.setText("Read complete%s — choose where to save it." % how
                               if self._standalone
                               else "Read complete%s — into a project." % how)
            name = "ECU-read-%s" % datetime.datetime.now().strftime("%H%M%S")
            self._hand_over(self._window.load_from_image, result, name)
        elif op == "restore":
            self.stage.setText("Flash and EEPROM written.")
            self._flashed("flash + EEPROM")
            QtWidgets.QMessageBox.information(
                self, "Flashing", "Flash sectors and EEPROM written and verified.")
        elif op == "write":
            self.stage.setText("Write over TP 2.0 complete." if self._ecu() == "ZF6HP"
                               else "Write complete.")
            self._flashed("write")
            took = ("" if self._last_elapsed is None
                    else " in %s" % self._fmt_elapsed(self._last_elapsed))
            # EDC16 NAKs the soft ECU-reset (7F 11 90); it needs an ignition cycle.
            msg = "Write complete%s." % took
            if self._ecu() == "EDC16":
                msg += " Turn off the ignition for 5 seconds."
            QtWidgets.QMessageBox.information(self, "Flashing", msg)
        elif _gui16.done(self, op, result):
            pass
        elif op == "backup" and result is not None:
            flash_img, ee = result
            self.stage.setText("Backup read complete.")
            self._finish_backup(flash_img, ee)
        elif op == "ee_read" and result is not None:
            hook = getattr(self._window, "load_eeprom_read", None)
            if hook is not None:
                # into a project (the standalone flasher saves an .ovt)
                self.stage.setText("EEPROM read complete.")
                name = "ECU-EEPROM-%s" % datetime.datetime.now().strftime(
                    "%Y%m%d-%H%M%S")
                self._hand_over(hook, bytes(result), name)
            else:
                self.stage.setText("EEPROM read complete — opening in the "
                                   "EEPROM editor.")
                self._open_eeprom_editor(result)
        elif op == "ee_write":
            self.stage.setText("EEPROM written and verified.")
            self._flashed("EEPROM")
            QtWidgets.QMessageBox.information(
                self, "Write EEPROM",
                "EEPROM written and verified.\n\n%s" % _info_text(_edc15.eeprom_info(result)))

    def _finish_backup(self, flash_img, ee):
        """A full backup (flash + EEPROM) is one read: it opens in the app as
        a new project whose original is the read -- or, in the standalone
        flasher, is saved as an .ovt project."""
        name = "EDC15-backup-%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._hand_over(lambda: self._window.load_backup(flash_img, ee, name=name))

    def _open_eeprom_editor(self, data):
        name = "ECU-EEPROM-%s (unsaved)" % datetime.datetime.now().strftime("%H%M%S")
        from .. import gui_eeprom
        if not gui_eeprom.layouts_for(bytes(data)):
            # No module knows it: the read is still the user's -- save it as is.
            from .. import recent as _recent
            path, _f = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save the EEPROM read (%s)"
                % _registry.missing_text("edc15", "the EEPROM editor"),
                os.path.join(_recent.get_last_dir("eeprom"), name.split(" ")[0] + ".bin"),
                "EEPROM images (*.bin);;All files (*)")
            if path:
                with open(path, "wb") as fh:
                    fh.write(bytes(data))
                _recent.set_last_dir(os.path.dirname(path), "eeprom")
            self.accept()
            return
        parent = self.parentWidget()

        def _edit():
            dlg = gui_eeprom.open_editor(parent, data, name=name)
            if dlg is not None:
                dlg.exec()
        self._hand_over(_edit)

    # ---- VDO cluster (K-line EEPROM) ----
    def _vdo_image(self):
        """(eeprom bytes, session) for the open cluster file, checksums
        corrected in the copy that is written -- or raise with the reason."""
        prim = getattr(self._window, "primary", None)
        if prim is None:
            raise RuntimeError("Open or read a cluster EEPROM (.vdo or a 2 KB .bin) first.")
        if getattr(prim, "platform", "") != "vdo":
            raise RuntimeError("The open file is not a VDO cluster EEPROM.")
        data = bytes(prim.binfile.data)
        cs = (_registry.checksum("vdo", prim)
              if getattr(prim, "support", "full") == "full"
              else self._checksum_or_confirm("vdo"))
        return (self._correct_and_check("VDO cluster", cs, data)
                if cs is not None else data), prim

    def _start_vdo(self, op, port, can):
        if can:
            QtWidgets.QMessageBox.warning(
                self, "Cluster", "The VDO cluster is K-line only: choose the K-line "
                "transport and a K-line cable or J2534 device.")
            return
        if op not in ("read", "write"):
            QtWidgets.QMessageBox.warning(self, "Cluster",
                                          "Only Read and Write apply to the cluster.")
            return
        image = None
        if op == "write":
            try:
                image, prim = self._vdo_image()
            except _WriteCancelled:
                return
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Write cluster", str(ex))
                return
            edited = _vdo.changed_parameters(bytes(prim.original), image)
            ident = getattr(prim, "vdo_identity", None)
            what = ("%s (%s)" % (prim.family, ident.label) if ident
                    else prim.family or prim.binfile.name)
            lines = ("\n  ".join(edited) if edited else "(nothing edited since the "
                     "file was opened)")
            if QtWidgets.QMessageBox.question(
                    self, "Write cluster",
                    "Write the open cluster EEPROM (%s) to the cluster?\n\n"
                    "Changed in this file since it was opened:\n  %s"
                    % (what, lines)) \
                    != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._op = op
        self._save_settings()
        self.bar.setValue(0)
        self._busy(True)
        self._worker = _Worker(op, VDO_ECU, port, self._speed(), image=image,
                               transport="kline", fast_init=False,
                               vdo_verify=self.vdo_verify_chk.isChecked())
        self._worker.vdo_mismatch.connect(self._vdo_mismatch_prompt)
        self._worker.stage.connect(self.stage.setText)
        self._worker.percent.connect(self.bar.setValue)
        self._worker.finished_ok.connect(lambda r: self._done(op, r))
        self._worker.failed.connect(self._error)
        self._worker.start()

    def _vdo_mismatch_prompt(self, file_type, cluster_type, ident):
        """GUI thread: the file is not this cluster's type. Cancel is the
        default; the worker keeps the session alive until this answers."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Cluster type mismatch")
        box.setText("This file is a %s EEPROM, but the connected cluster is "
                    "%s." % (file_type, cluster_type))
        box.setInformativeText(
            "Cluster: %s\n\nWriting another family's EEPROM can leave the "
            "cluster unusable. The connection is held open while you "
            "decide." % (ident or "ident unknown"))
        go = box.addButton("Write anyway", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        w = self._worker
        if w is not None:
            w.answer_vdo_mismatch(box.clickedButton() is go)

    def _done_vdo(self, op, result):
        kind = result.get("_vdo")
        if kind == "read":
            from ..platforms import vdo_container as _cv
            meta = result.get("meta") or {}
            blob = _cv.container_bytes(result["eeprom"], meta)
            ver = (meta.get("rom_version") or "cluster").replace(" ", "_")
            name = "Cluster-%s-%s" % (ver, datetime.datetime.now().strftime("%H%M%S"))
            self.stage.setText("Cluster read: %s" % (meta.get("rom_version") or "done"))
            # into a project (the standalone flasher saves it as an .ovt)
            self._hand_over(self._window.load_from_image, blob, name)
            return
        rep = result.get("report")
        if getattr(rep, "cancelled", False):
            self.stage.setText("Cluster write cancelled; nothing written.")
            QtWidgets.QMessageBox.information(self, "Write cluster", rep.describe())
            return
        self.stage.setText("Cluster write complete.")
        self._flashed("cluster EEPROM")
        # a cluster that was sent the reset restarts by itself; otherwise the
        # report already says to switch the ignition off and on
        QtWidgets.QMessageBox.information(self, "Write cluster", rep.describe())

    #: operations that write to the control unit; when one fails at a speed
    #: above the lowest, the message suggests the next one down
    _WRITE_OPS = ("write", "ee_write", "restore", "write_program")

    def _lower_speed_hint(self, msg):
        """``msg`` with a suggestion to try the next lower speed, when a
        write failed and the speed list (K-line EDC15 / EDC16 only) has one.
        Unchanged for a Cancel, for a read, and when the speed picked is
        already the lowest."""
        if getattr(self, "_op", None) not in self._WRITE_OPS:
            return msg
        if (msg or "").strip().rstrip(".") == "Stopped":
            return msg
        if not self._speed_applies() or self.boot_mode.isChecked():
            return msg                       # no speed is chosen for these
        i = self.speed_box.currentIndex()
        if i <= 0:
            return msg
        lower = self.speed_box.itemText(i - 1)
        return "%s\n\nIf this keeps failing, try a lower speed (%s) in the " \
               "Speed list: a slower link is more forgiving of a noisy cable " \
               "or a weak connection." % (msg.rstrip(), lower)

    def _error(self, msg):
        self._busy(False)
        msg = self._lower_speed_hint(msg)
        self.stage.setText("Failed: %s" % msg.split("\n\n")[0])
        QtWidgets.QMessageBox.critical(self, "Flashing", msg)


def _info_text(info):
    return ("VIN: %s\nImmobiliser: %s (%s)\nImmo ID: %s\nSKC: %05d\nOdometer: %.2f km"
            % (info["vin"], "on" if info["immo_on"] else "off", info["immo_number"],
               info["immo_id"], info["skc"], info["odometer_km"]))
