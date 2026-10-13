# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ISO 15765-2 ("ISO-TP") transport over a raw CAN channel.

This is the proven tester-side ISO-TP engine formerly defined inline in
:mod:`ovtsuite.flash.zf6hp_uds`.  It was lifted into its own module with a
deliberately minimal import graph -- only the standard library plus the shared
``KLineError``/``KLineTimeout`` -- so the 32-bit J2534 bridge
(:mod:`ovtsuite.flash.j2534_bridge`) can import and run it IN-PROCESS next to
the DLL, exactly the way it already runs :mod:`ovtsuite.flash.tp20_transport`.

Running the ISO-TP reassembly inside the bridge turns a calibration read from
~35 per-frame IPC round-trips per chunk (the dominant cost over the 32-bit
bridge, and the reason a 512 KB read crawled and stalled) into ONE IPC per
chunk: the bridge writes the request, drains and reassembles the whole response
against the DLL at CAN speed, and hands back the finished bytes.  :class:`IsoTp`
is unchanged behaviour -- :mod:`ovtsuite.flash.zf6hp_uds` re-exports it, and all
existing tester-side callers keep using ``zf6hp_uds.IsoTp``.
"""
import time
from typing import Dict, Optional, Tuple

try:                                    # the app: shared transport exceptions
    from .transport import KLineError, KLineTimeout
except Exception:                       # pragma: no cover - standalone (e.g. the 32-bit bridge)
    try:
        from transport import KLineError, KLineTimeout   # bundled beside the bridge
    except Exception:
        class KLineError(Exception):
            pass

        class KLineTimeout(KLineError):
            pass


# ---- default ids (standard VAG powertrain physical pair + functional) ----
REQ_ID = 0x7E1          # tester -> TCU (physical)
RESP_ID = 0x7E9         # TCU -> tester
FUNC_ID = 0x7DF         # functional broadcast (tester -> all)
PAD = 0x55              # byte VCDS pads short CAN frames with on this car

# ---- ISO-TP protocol control information (PCI) high nibbles ----
_PCI_SF = 0x0           # single frame
_PCI_FF = 0x1           # first frame
_PCI_CF = 0x2           # consecutive frame
_PCI_FC = 0x3           # flow control

# When the ECU's flow control allows a continuous stream (STmin=0) and the
# channel exposes write_can_many, consecutive frames are written this many at a
# time in a single PassThruWriteMsgs call -- amortizing the per-call DLL/USB
# latency that otherwise paces each CF (a 6HP28 write over the 32-bit bridge sat
# at ~2.4 ms/frame, all per-call, with the TCU asking for 0 ms). Order within a
# batch is preserved; batches never cross a flow-control (block-size) boundary.
_CF_BATCH = 16


def _precise_wait(seconds: float):
    """Busy-ish wait accurate below the OS scheduler quantum -- same approach
    as tp20_transport._precise_wait, duplicated here to keep this module's
    import graph independent of the TP 2.0 stack."""
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    if seconds > 0.002:
        time.sleep(seconds - 0.001)
    while time.perf_counter() < end:
        pass


class IsoTp:
    """ISO 15765-2 over a raw CAN channel.

    ``can`` is any object exposing ``write_can(can_id, payload, timeout_ms=)``
    and ``read(max_msgs, timeout_ms) -> [msg]`` with ``msg.can_id`` /
    ``msg.payload`` -- i.e. :class:`ovtsuite.flash.j2534.Channel`,
    :class:`ovtsuite.flash.openport.OpenPortChannel`, or a test double.  This
    is the SAME surface :class:`ovtsuite.flash.tp20_transport.Tp20Channel`
    consumes, so either hardware path works unchanged.

    Frames on ids other than ``rx_id`` are discarded on the spot (the vehicle
    broadcast flood would otherwise swamp the buffer -- the same reasoning as
    Tp20Channel._next_frame).
    """

    def __init__(self, can, tx_id=REQ_ID, rx_id=RESP_ID, pad=PAD, log=None,
                 is_stop=None, st_min_ms=1, block_size=0, poll_spin_ms=0.3):
        self.can = can
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.pad = pad
        self._log = log or (lambda s: None)
        self.is_stop = is_stop
        self.tx_st_min_ms = st_min_ms   # STmin we advertise in our flow control
        self.tx_block_size = block_size  # BS we advertise (0 = send all, no further FC)
        self._poll_spin_ms = poll_spin_ms
        self._pending = []              # frames read but not yet consumed

    # ---- id filtering ----
    def pass_ids(self):
        """Ask the device to admit only our rx id (+ nothing else).  No-op on
        channels without hardware filtering; those rely on the software drop."""
        fn = getattr(self.can, "pass_ids", None)
        if fn is None:
            return
        try:
            fn([self.rx_id])
        except Exception as ex:          # pragma: no cover - hardware dependent
            self._log("ISO-TP: hardware filter unavailable (%s); software drop only" % ex)
            pa = getattr(self.can, "pass_all", None)
            if pa is not None:
                try:
                    pa()
                except Exception:
                    pass

    # ---- raw frame io ----
    def _pad(self, data: bytes) -> bytes:
        frame = bytes(data)
        if len(frame) < 8:
            frame = frame + bytes([self.pad]) * (8 - len(frame))
        return frame

    def _write_frame(self, data: bytes, can_id=None):
        self.can.write_can(self.tx_id if can_id is None else can_id, self._pad(data))

    def _next_frame(self, timeout_ms: int) -> Optional[bytes]:
        deadline = time.monotonic() + timeout_ms / 1000.0
        polled = False
        while True:
            if self.is_stop is not None and self.is_stop():
                raise KLineError("ISO-TP: aborted (stop requested)")
            for i, m in enumerate(self._pending):
                if m.can_id == self.rx_id:
                    del self._pending[i]
                    return bytes(m.payload)
            if time.monotonic() >= deadline and polled:
                return None
            polled = True
            got = self.can.read(16, 0)
            if got:
                self._pending.extend(m for m in got if m.can_id == self.rx_id)
            elif time.monotonic() < deadline:
                _precise_wait(self._poll_spin_ms / 1000.0)

    # ---- message io ----
    def send(self, payload: bytes, can_id=None):
        """Send one UDS request as an ISO-TP message (single or segmented)."""
        payload = bytes(payload)
        if len(payload) <= 7:
            self._write_frame(bytes([(_PCI_SF << 4) | len(payload)]) + payload, can_id)
            return
        # first frame: 12-bit length, 6 data bytes
        n = len(payload)
        if n > 0xFFF:
            raise KLineError("ISO-TP: message too long (%d bytes)" % n)
        self._write_frame(bytes([(_PCI_FF << 4) | (n >> 8), n & 0xFF]) + payload[:6], can_id)
        # wait for the TCU's flow control
        bs, st_min = self._await_fc()
        cid = self.tx_id if can_id is None else can_id
        # Batch the consecutive-frame stream when the ECU asked for no inter-frame
        # delay (STmin=0) and the channel can write several frames per call --
        # this is what removes the ~2.4 ms-per-frame per-call latency on a write.
        # Falls back to the exact per-frame path otherwise (direct devices,
        # STmin>0, or channels/test doubles without write_can_many), so timing
        # behaviour is unchanged wherever batching does not apply.
        batch_writer = getattr(self.can, "write_can_many", None)
        idx = 6
        sn = 1
        sent_since_fc = 0
        while idx < n:
            if batch_writer is not None and st_min == 0:
                room = _CF_BATCH if not bs else min(_CF_BATCH, bs - sent_since_fc)
                frames = []
                while idx < n and len(frames) < room:
                    frames.append(self._pad(bytes([(_PCI_CF << 4) | (sn & 0x0F)])
                                            + payload[idx:idx + 7]))
                    idx += 7
                    sn = (sn + 1) & 0x0F
                    sent_since_fc += 1
                batch_writer(cid, frames)
            else:
                self._write_frame(bytes([(_PCI_CF << 4) | (sn & 0x0F)]) + payload[idx:idx + 7], can_id)
                idx += 7
                sn = (sn + 1) & 0x0F
                sent_since_fc += 1
                if idx < n and st_min:
                    _precise_wait(st_min / 1000.0)
            if bs and sent_since_fc >= bs and idx < n:
                bs, st_min = self._await_fc()
                sent_since_fc = 0

    def _await_fc(self, timeout_ms=1000) -> Tuple[int, float]:
        """Wait for a flow-control frame; return (block_size, st_min_ms)."""
        while True:
            frame = self._next_frame(timeout_ms)
            if frame is None:
                raise KLineTimeout("ISO-TP: no flow control from TCU")
            if (frame[0] >> 4) != _PCI_FC:
                continue                 # ignore anything that is not an FC here
            flow = frame[0] & 0x0F
            if flow == 1:                # wait -- keep waiting for a CTS
                continue
            if flow == 2:
                raise KLineError("ISO-TP: flow control overflow/abort from TCU")
            bs = frame[1] if len(frame) > 1 else 0
            raw = frame[2] if len(frame) > 2 else 0
            st_min = raw if raw <= 0x7F else (raw - 0xF0) / 1000.0 if 0xF1 <= raw <= 0xF9 else 0
            return bs, st_min

    def recv(self, timeout_ms=2000) -> bytes:
        """Receive one complete UDS response message, reassembling ISO-TP."""
        frame = self._next_frame(timeout_ms)
        if frame is None:
            raise KLineTimeout("ISO-TP: no response from TCU")
        pci = frame[0] >> 4
        if pci == _PCI_SF:
            length = frame[0] & 0x0F
            return bytes(frame[1:1 + length])
        if pci == _PCI_FF:
            length = ((frame[0] & 0x0F) << 8) | frame[1]
            data = bytearray(frame[2:])
            # send flow control: clear to send, our BS / STmin
            self._write_frame(bytes([(_PCI_FC << 4) | 0, self.tx_block_size, self.tx_st_min_ms]))
            sn = 1
            while len(data) < length:
                cf = self._next_frame(timeout_ms)
                if cf is None:
                    raise KLineTimeout("ISO-TP: consecutive frame timeout")
                if (cf[0] >> 4) != _PCI_CF:
                    continue
                if (cf[0] & 0x0F) != (sn & 0x0F):
                    self._log("ISO-TP: sequence gap (got %d want %d)" % (cf[0] & 0x0F, sn & 0x0F))
                data += cf[1:]
                sn = (sn + 1) & 0x0F
            return bytes(data[:length])
        # a flow-control or consecutive frame with no message in progress: skip it
        return self.recv(timeout_ms)

    # ---- promiscuous discovery (gateway-routed id unknown) ----
    @staticmethod
    def req_for_response(resp_id: int) -> int:
        """Best-guess physical REQUEST id for a given RESPONSE id, covering the
        two VAG schemes this bus actually uses:
          * ISO 15765-4 OBD: response 0x7E8..0x7EF -> request response-8
            (engine 0x7E8<-0x7E0, trans 0x7E9<-0x7E1).
          * VAG UDS (gateway): response = request + 0x6A
            (airbag 0x77F<-0x715, confirmed in Reference/Logs/VCDS-Autoscan.log).
        Used to address the flow-control frame back to a responder during a
        functional scan, and reported so the caller knows which id to drive the
        full probe on."""
        if 0x7E8 <= resp_id <= 0x7EF:
            return resp_id - 8
        if resp_id > 0x6A:
            return resp_id - 0x6A
        return resp_id

    def functional_scan(self, request: bytes, listen_ms: int = 500,
                        func_id: int = FUNC_ID) -> Dict[int, bytes]:
        """Send one functional request to ``func_id`` (0x7DF) and collect every
        physical response, keyed by the CAN id it came back on.

        This is the ISO-TP equivalent of reading the gateway-assigned id out of
        a TP 2.0 setup reply: instead of assuming where the TCU answers, we ask
        the whole bus and see who replies and on which id. Listens
        promiscuously (pass_all), so it needs the in-process CAN channel, not a
        single-id hardware filter. Handles single-frame answers directly and
        reassembles multi-frame ones (sending the flow control back to each
        responder's own request id via :meth:`req_for_response`)."""
        pa = getattr(self.can, "pass_all", None)
        if pa is not None:
            try:
                pa()
            except Exception as ex:          # pragma: no cover - hardware dependent
                self._log("ISO-TP scan: pass_all unavailable (%s)" % ex)
        clear = getattr(self.can, "clear_rx", None)
        if clear is not None:
            try:
                clear()
            except Exception:
                pass
        self._pending = []
        request = bytes(request)
        if len(request) > 7:
            raise KLineError("ISO-TP scan: functional request must be a single frame")
        self._write_frame(bytes([(_PCI_SF << 4) | len(request)]) + request, can_id=func_id)
        partial: Dict[int, list] = {}
        done: Dict[int, bytes] = {}
        deadline = time.monotonic() + listen_ms / 1000.0
        while time.monotonic() < deadline:
            if self.is_stop is not None and self.is_stop():
                break
            got = self.can.read(16, 0)
            if not got:
                _precise_wait(self._poll_spin_ms / 1000.0)
                continue
            for m in got:
                cid = m.can_id
                d = bytes(m.payload)
                if not d or cid == func_id:
                    continue
                pci = d[0] >> 4
                if pci == _PCI_SF:
                    n = d[0] & 0x0F
                    done.setdefault(cid, bytes(d[1:1 + n]))
                elif pci == _PCI_FF:
                    n = ((d[0] & 0x0F) << 8) | d[1]
                    partial[cid] = [n, bytearray(d[2:])]
                    # clear-to-send, addressed to THIS responder's request id
                    self._write_frame(
                        bytes([(_PCI_FC << 4), self.tx_block_size, self.tx_st_min_ms]),
                        can_id=self.req_for_response(cid))
                elif pci == _PCI_CF and cid in partial:
                    exp, buf = partial[cid]
                    buf += d[1:]
                    if len(buf) >= exp:
                        done.setdefault(cid, bytes(buf[:exp]))
                        del partial[cid]
        return done
