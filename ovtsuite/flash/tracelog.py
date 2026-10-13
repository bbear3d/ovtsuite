# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Optional wire-level debug logging for a flash/log session.

Records every frame or byte the transport exchanges to a capture file, so a
failed CAN or K-line session can be inspected offline (the CAN log loads
straight back into the Bus Sniffer -- it is the same
:mod:`ovtsuite.sniff.capture` format).  This is a debugging aid, wired to an
opt-in checkbox in the flash dialog; it is expected to be removed or hidden
later.

* :class:`FrameTap` wraps a CAN ``Channel`` (j2534 / openport): it delegates
  every call unchanged and logs each transmitted frame as ``can-tx`` and each
  received frame as ``can``.
* :func:`kline_trace` returns a callback the pyserial :class:`KLine` calls with
  ``("tx"/"rx", bytes)``; it writes ``kraw`` / ``kraw-tx`` byte bursts.
"""
import time
from typing import Optional

from ..sniff import capture as _cap


def open_writer(path: str, header: str = "") -> _cap.Writer:
    return _cap.Writer(path, header)


class FrameTap:
    """Wraps a CAN channel, teeing every frame to a capture Writer.  Presents
    the same surface Tp20Channel uses (write_can / read / pass_all /
    set_config / set_baud / clear_rx / clear_tx / add_filter / disconnect)."""

    def __init__(self, channel, writer: _cap.Writer):
        self._ch = channel
        self._w = writer
        self.protocol = getattr(channel, "protocol", None)

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        try:
            self._w.add(_cap.Record(time.time(), "can", int(can_id), bytes(payload),
                                    tx=True, extended=extended))
        except Exception:
            pass
        return self._ch.write_can(can_id, payload, extended, timeout_ms)

    def read(self, max_msgs=16, timeout_ms=100):
        msgs = self._ch.read(max_msgs, timeout_ms)
        for m in msgs:
            try:
                cid = m.can_id if m.can_id is not None else 0
                self._w.add(_cap.Record(time.time(), "can", int(cid), bytes(m.payload),
                                        tx=bool(getattr(m, "is_tx_echo", False)),
                                        extended=bool(getattr(m, "tx_flags", 0) & 0x100)))
            except Exception:
                pass
        return msgs

    # -- straight passthroughs --
    def __getattr__(self, name):
        return getattr(self._ch, name)


def kline_trace(writer: _cap.Writer):
    """Return an on_trace(direction, data) callback for :class:`KLine`."""
    def cb(direction: str, data: bytes):
        try:
            writer.add(_cap.Record(time.time(), "kraw", 0, bytes(data),
                                   tx=(direction == "tx")))
        except Exception:
            pass
    return cb


def close_writer(writer: Optional[_cap.Writer]):
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass


__all__ = ["FrameTap", "open_writer", "kline_trace", "close_writer"]
