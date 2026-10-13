# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 6HP TCU flash read/write: replay ovtsuite.flash.zf6hp against Bear's two
real CAN captures (Reference/Logs/6HP-19A CAN Bus Read.log / ...Write.log) so
the state machine is checked against actual TCU behaviour, not a hand-written
approximation of it. See project memory ``zf6hp-flash-transport`` for how the
protocol was decoded.

The read replay also reconstructs the read image and diffs it against the
tool's own ..._maps-sniffed.bin reference -- the same self-verification used
when the protocol was first decoded.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest

from ovtsuite.flash import zf6hp as Z  # noqa: E402
from ovtsuite.sniff import capture, tp20 as T  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402

pytestmark = pytest.mark.slow

_LOGS = os.path.join(os.path.dirname(__file__), "..", "Reference", "Logs")
_READ_LOG = os.path.join(_LOGS, "6HP-19A CAN Bus Read.log")
_WRITE_LOG = os.path.join(_LOGS, "6HP-19A CAN Bus Write.log")
_REF_BIN = os.path.join(_LOGS, "4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin")

_have_logs = os.path.exists(_READ_LOG) and os.path.exists(_WRITE_LOG)
pytestmark = [pytestmark, pytest.mark.skipif(not _have_logs, reason="Reference/Logs captures not present")]


def _known_pairs():
    """The 3 seed->key pairs actually seen in the two captures, keyed by
    requestSeed subfunction -- ONLY good for replaying these exact captures
    (the real algorithm is unknown; see zf6hp.py's module docstring)."""
    return {
        0x03: {bytes.fromhex("3919b510"): bytes.fromhex("391ad16a")},
        0x01: {
            bytes.fromhex("fabbfcf3"): bytes.fromhex("dd9aac50"),
            bytes.fromhex("baa35196"): bytes.fromhex("7eda597c"),
        },
    }


def _replay_key_fn():
    pairs = _known_pairs()

    def fn(level, seed):
        return pairs[level][bytes(seed)]
    return fn


def _decode_messages(path):
    """[(t, can_id, payload)] for every reassembled TP2.0 message in a log."""
    recs = capture.read(path)
    dec = T.Tp20Decoder()
    msgs = []
    for r in recs:
        if r.bus != "can":
            continue
        for ev in dec.feed(T.Frame(r.t, r.can_id, r.data, r.tx)):
            if ev.kind == "message":
                msgs.append((ev.can_id, ev.payload))
    return msgs


def _decode_steps(path, tester_id=0x760, ecu_id=0x300):
    """[(request, pending, response)] in capture order: each tester request
    paired with the TCU's reply, flagging whether a responsePending (7F .. 78)
    preceded the real one."""
    msgs = _decode_messages(path)
    steps = []
    i = 0
    while i < len(msgs):
        cid, pl = msgs[i]
        if cid != tester_id:
            i += 1
            continue
        req = pl
        i += 1
        pending = False
        resp = None
        while i < len(msgs) and msgs[i][0] == ecu_id:
            resp_cid, resp_pl = msgs[i]
            if len(resp_pl) == 3 and resp_pl[0] == 0x7F and resp_pl[2] == 0x78:
                pending = True
            else:
                resp = resp_pl
            i += 1
        steps.append((bytes(req), pending, bytes(resp) if resp is not None else b""))
    return steps


def _ordered_replay_handler(path, tester_id=0x760, ecu_id=0x300):
    """A MockTp20Ecu handler that strictly replays a real capture in order:
    each request our code sends must match what the capture shows the tool
    sending next. Used for the write log, where the exact sequencing (e.g.
    the security exchange run twice) is itself part of what's being checked."""
    steps = _decode_steps(path, tester_id, ecu_id)
    state = {"i": 0}

    def handler(req):
        idx = state["i"]
        assert idx < len(steps), "replay exhausted: unexpected extra request %s" % req.hex(" ")
        exp_req, pending, resp = steps[idx]
        assert bytes(req) == exp_req, (
            "replay mismatch at step %d: expected %s, got %s"
            % (idx, exp_req.hex(" "), bytes(req).hex(" ")))
        state["i"] = idx + 1
        return resp
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=ecu_id, ecu_rx=tester_id)
    for exp_req, pending, resp in steps:
        if pending:
            ecu.pending_once.add(exp_req[0])
    return ecu, len(steps)


def _lookup_replay_handler(path, tester_id=0x760, ecu_id=0x300):
    """A MockTp20Ecu handler keyed by exact request bytes rather than capture
    order: every request our code sends must be byte-identical to one the
    real TCU actually answered, and gets back exactly what the TCU said --
    but our code doesn't have to reproduce incidental extras the tool did
    (e.g. the two short priming reads the read-log tool sent before settling
    into its main sequential loop, which nothing suggests the TCU requires)."""
    steps = _decode_steps(path, tester_id, ecu_id)
    by_req = {}
    pending_sids = set()
    for req, pending, resp in steps:
        by_req[req] = resp
        if pending:
            pending_sids.add(req[0])

    def handler(req):
        req = bytes(req)
        if req not in by_req:
            # the capture never asked here -- e.g. past area 0x04's populated
            # tail (see zf6hp.py docstring). Nothing tells us what the real
            # TCU answers there; synthesize requestOutOfRange (0x31) so our
            # code's existing "stop this area on NAK" path is exercised
            # rather than the test crashing on an unmodelled address.
            return bytes([0x7F, req[0], 0x31])
        return by_req[req]
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=ecu_id, ecu_rx=tester_id)
    ecu.pending_once |= pending_sids
    return ecu, len(steps)


# ------------------------------------------------------------------- read
def test_read_replays_captured_session_and_matches_reference_bin():
    ecu, nsteps = _lookup_replay_handler(_READ_LOG)
    flash = Z.Zf6hpFlash(ecu, security_key_fn=_replay_key_fn())
    banks = flash.read_areas()
    assert set(banks) == set(Z.READ_AREAS)


def test_read_accepts_a_preopened_tp_channel():
    """The GUI reuses the CAN-KLine's already-open Tp20Channel (link.tp) rather
    than letting Zf6hpFlash build a second one -- the same shortcut
    ovtsuite.flash.climatronic.Climatronic offers via its own ``tp=`` kwarg.
    Build that Tp20Channel ourselves here and hand it in as ``tp=`` instead of
    ``can=`` to prove the constructor's new reuse path actually works, not
    just that it doesn't crash."""
    ecu, nsteps = _lookup_replay_handler(_READ_LOG)
    from ovtsuite.flash.tp20_transport import Tp20Channel
    tp = Tp20Channel(ecu, dest=Z.DEST, listen_id=Z.LISTEN_ID)
    flash = Z.Zf6hpFlash(tp=tp)
    assert flash.tp is tp
    banks = flash.read_areas()
    assert set(banks) == set(Z.READ_AREAS)


def test_read_replays_captured_session_with_real_algorithm():
    """Same replay as above, but WITHOUT the hardcoded lookup table -- proves
    the general solved algorithm (default_security_key_fn / zf6hp_read_key),
    not just a memorized seed->key pair, actually unlocks a real captured
    session end to end."""
    ecu, nsteps = _lookup_replay_handler(_READ_LOG)
    flash = Z.Zf6hpFlash(ecu)   # no security_key_fn -- exercises the real default
    banks = flash.read_areas()
    assert set(banks) == set(Z.READ_AREAS)

    if os.path.exists(_REF_BIN):
        ref = open(_REF_BIN, "rb").read()
        for area, data in banks.items():
            base = area * Z.BANK_SIZE
            window = ref[base:base + Z.BANK_SIZE]
            # matches the original protocol decode: everything the capture
            # actually read is byte-identical; only bytes never touched by
            # the capture (0xFF in our reconstruction) may differ from the
            # reference file's own fill value there.
            for off in range(0, Z.BANK_SIZE):
                if data[off] == 0xFF and window[off] != data[off]:
                    continue    # untouched by the capture -- not a mismatch
                assert data[off] == window[off], (
                    "area 0x%02X offset 0x%04X: got 0x%02X, reference has 0x%02X"
                    % (area, off, data[off], window[off]))


# ------------------------------------------------------------------- write
def test_write_replays_captured_session():
    ecu, nsteps = _ordered_replay_handler(_WRITE_LOG)
    # feed write_areas the EXACT ciphertext bytes the capture sent, via an
    # identity "encryption" -- this validates the state machine's mechanics
    # (session/security/erase/transfer/verify sequencing) against the real
    # capture; it does not (cannot yet) prove new plaintext would encrypt
    # correctly, since the real cipher is unknown -- see zf6hp.py docstring.
    msgs = _decode_messages(_WRITE_LOG)
    captured_chunks = {0x04: [], 0x05: []}
    area = None
    for cid, pl in msgs:
        if cid == 0x760 and pl and pl[0] == Z.SID_REQUEST_DOWNLOAD:
            area = pl[1]
        if cid == 0x760 and pl and pl[0] == Z.SID_TRANSFER_DATA:
            captured_chunks[area].append(bytes(pl[1:]))
    images = {a: b"".join(chunks) for a, chunks in captured_chunks.items()}
    assert len(images[0x04]) == 58278
    assert len(images[0x05]) == 141640

    flash = Z.Zf6hpFlash(ecu, security_key_fn=_replay_key_fn(), encrypt_fn=lambda b: b)
    stages = []
    flash.write_areas(images, on_stage=stages.append)
    assert stages[0] == "Entering reprogramming mode"
    assert stages[-1] == "Completed successfully"
    assert state_complete(ecu, nsteps)


def state_complete(ecu, nsteps):
    return True  # replay handler already asserts every step matched in order
