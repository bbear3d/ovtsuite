# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Zf6hpFlash.write_areas() mechanics, exercised against arbitrary (NOT
captured) plaintext via a permissive synthetic mock ECU -- deliberately kept
in its own file, separate from test_flash_zf6hp.py's capture-replay tests,
because it needs none of the real CAN log files those require (and must
never be silently skipped by that file's log-presence gate: this is the
only test that actually proves the write_areas per-area encrypt_fn plumbing
introduced alongside the BCB encoder, see ovtsuite/flash/zf6hp.py's module
docstring item 2 and project memory ``zf6hp-flash-transport``), and (as of
2026-09-21) the per-area checksum pre-check / dynamic post-write verify
added in the same memory entry's GUI WRITE WIRING section."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import zf6hp as Z  # noqa: E402
from ovtsuite.flash.crypto import zf6hp_area_checksum, zf6hp_bcb_decode  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402


def _synthetic_write_handler(initial_content=None, reject_first_checksum_probe=False):
    """A permissive (not capture-replaying) KWP handler for exercising
    write_areas' MECHANICS with arbitrary plaintext/encrypt_fn combinations
    that don't match any one real capture's exact bytes. Verifies the write
    -level security exchange against the REAL algorithm
    (default_security_key_fn), acks everything else structurally (matching
    what write_areas actually checks -- see the SID-by-SID notes in this
    function), and records the raw transferData bytes sent per area so the
    test can decode them back and check against the original plaintext.

    Also simulates a checksum-aware ECU well enough to test the pre-write
    skip / dynamic post-write verify: ``current`` (returned alongside the
    handler) is a mutable ``area -> plaintext`` dict standing in for "what's
    actually on the TCU's flash right now" (seed it via ``initial_content``,
    or let a real simulated write update it via requestTransferExit below).
    Any routine-0xC5 (checksum) request -- pre-write probe OR post-write
    verify, they're the same request shape, see
    zf6hp.py's ``_checksum_routine_params`` -- is answered by comparing its
    requested checksum against ``zf6hp_area_checksum(current[area])``, so a
    test can seed ``current`` to make a pre-check report "match" (or write
    once for real and let this update it, then write the same data again),
    without having to fake the wire protocol by hand.

    ``reject_first_checksum_probe``: makes exactly the FIRST routine-0xC5
    start request come back NEGATIVE (as if the TCU doesn't support/accept
    this probe at all), to exercise write_areas' fail-safe fallback -- the
    second one succeeds normally, so this only ever rejects a PRE-write
    probe (write_areas' post-write verify is always the LAST 0xC5 call for
    a given area, and there's exactly one probe per area before it)."""
    state = {"last_seed": None, "cur_area": None,
             "last_c5_area": None, "last_c5_checksum": None, "c5_start_calls": 0}
    captured = {0x04: [], 0x05: []}
    current = dict(initial_content or {})

    def handler(req):
        req = bytes(req)
        sid = req[0]
        if sid == Z.SID_START_SESSION:
            return bytes([sid | Z.POS, req[1]])
        if sid == Z.SID_SECURITY:
            sub = req[1]
            if sub == Z.LEVEL_WRITE[0]:
                seed = os.urandom(4)
                state["last_seed"] = seed
                return bytes([sid | Z.POS, sub]) + seed
            if sub == Z.LEVEL_WRITE[1]:
                expected = Z.default_security_key_fn(Z.LEVEL_WRITE[0], state["last_seed"])
                got = req[2:]
                assert got == expected, (
                    "synthetic handler: wrong write key -- got %s expected %s"
                    % (got.hex(), expected.hex()))
                return bytes([sid | Z.POS, sub])
        if sid == Z.SID_REQUEST_DOWNLOAD:
            area = req[1]
            state["cur_area"] = area
            captured[area] = []          # a fresh download starts a fresh capture for this area
            return bytes([sid | Z.POS])
        if sid == Z.SID_START_ROUTINE:              # erase (0xC4) or checksum (0xC5) start
            routine = req[1]
            if routine == Z.ROUTINE_VERIFY and len(req) >= 10:
                state["c5_start_calls"] += 1
                if reject_first_checksum_probe and state["c5_start_calls"] == 1:
                    return bytes([0x7F, sid, 0x22])   # conditionsNotCorrect-style NAK
                start = (req[2] << 16) | (req[3] << 8) | req[4]
                state["last_c5_area"] = start // Z.BANK_SIZE
                state["last_c5_checksum"] = (req[8] << 8) | req[9]
            return bytes([sid | Z.POS, routine])
        if sid == Z.SID_ROUTINE_RESULTS:
            routine = req[1]
            if routine == Z.ROUTINE_VERIFY:
                area = state["last_c5_area"]
                actual = zf6hp_area_checksum(current.get(area, b""))
                status = 0x00 if actual == state["last_c5_checksum"] else 0x01
                return bytes([sid | Z.POS, routine, status])
            return bytes([sid | Z.POS, routine, 0x00])   # erase always "succeeds"
        if sid == Z.SID_TRANSFER_DATA:
            captured[state["cur_area"]].append(bytes(req[1:]))
            return bytes([sid | Z.POS])
        if sid == Z.SID_TRANSFER_EXIT:
            # "Commit" the write into the mock's simulated flash so a later
            # checksum probe (this call's post-write verify, or a future
            # write_areas() call) sees the real content that was sent.
            area = state["cur_area"]
            wire = b"".join(captured[area])
            try:
                current[area] = zf6hp_bcb_decode(wire)
            except Exception:
                pass
            return bytes([sid | Z.POS])
        if sid == Z.SID_STOP_COMM:
            return bytes([sid | Z.POS])
        raise AssertionError("synthetic handler: unhandled request %s" % req.hex(" "))

    return handler, captured, current


def test_write_areas_calls_encrypt_fn_once_per_area_and_wire_matches_via_decode():
    """The plumbing fix: write_areas must call encrypt_fn ONCE per area with
    the area's FULL plaintext (matching how the real BCB cipher
    compresses+XORs a whole area as one continuous stream), then chunk
    encrypt_fn's OUTPUT into 254-byte transferData wire envelopes -- it must
    NOT call encrypt_fn once per 254-byte slice of the plaintext (that never
    matched the real wire format; envelope boundaries have no relation to
    plaintext chunk boundaries -- see zf6hp.py's module docstring item 2).

    Exercised against arbitrary (non-captured) plaintext specifically so a
    bug that only happens to work when plaintext and TRANSFER_CHUNK line up
    can't hide -- default_security_key_fn (the real algorithm, not a replay
    lookup) unlocks for real, and default_encrypt_fn (the real BCB encoder)
    does the encoding; the wire bytes actually sent are reassembled per area
    and decoded back with zf6hp_bcb_decode to confirm they are byte-exact
    the whole way from plaintext -> encoder -> wire chunks -> decoder. Runs
    with no seeded ``current`` content, so the pre-write checksum probe
    reports mismatch and a full write proceeds, same as before that probe
    existed."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)

    calls = []

    def spy_encrypt(plain):
        calls.append(bytes(plain))
        return Z.default_encrypt_fn(plain)

    # deliberately NOT a multiple of TRANSFER_CHUNK (254), and includes both an
    # RLE-worthy run and high-entropy data, so the plumbing has to hold up
    # under realistic-shaped content rather than a convenient size.
    plain_04 = (bytes([0xAB]) * 5000) + os.urandom(3001) + (bytes([0x00]) * 4000)
    plain_05 = os.urandom(2003)
    images = {0x04: plain_04, 0x05: plain_05}

    flash = Z.Zf6hpFlash(ecu, encrypt_fn=spy_encrypt)
    flash.write_areas(images)

    assert calls == [plain_04, plain_05], (
        "encrypt_fn must be called once per area, in WRITE_AREAS order, with the "
        "FULL plaintext each time -- not once per 254-byte chunk")

    for area, plain in images.items():
        wire_stream = b"".join(captured[area])
        assert zf6hp_bcb_decode(wire_stream) == plain, (
            "area 0x%02X: wire bytes actually sent don't decode back to the "
            "original plaintext" % area)


def test_write_areas_defaults_encrypt_fn_when_not_supplied():
    """Zf6hpFlash(can) with NO encrypt_fn at all must still write successfully
    -- encryption isn't optional for a 6HP write any more than it is for
    EDC16's (write_flash calls encrypt() unconditionally there too), so
    Zf6hpFlash defaults encrypt_fn to default_encrypt_fn the same way it
    already defaults security_key_fn. Mirrors
    test_read_replays_captured_session_with_real_algorithm in
    test_flash_zf6hp.py, which proves the same thing for the read-side
    default."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)

    plain_04 = os.urandom(1500)
    plain_05 = (bytes([0x00]) * 3000) + os.urandom(500)
    images = {0x04: plain_04, 0x05: plain_05}

    flash = Z.Zf6hpFlash(ecu)   # no encrypt_fn, no security_key_fn -- both real defaults
    assert flash.encrypt_fn is Z.default_encrypt_fn
    flash.write_areas(images)

    for area, plain in images.items():
        wire_stream = b"".join(captured[area])
        assert zf6hp_bcb_decode(wire_stream) == plain


def test_write_areas_skips_an_area_whose_checksum_already_matches():
    """The new per-area skip: write area 0x04 once for real (so the mock's
    simulated ``current`` content gets updated by the handler's
    requestTransferExit hook, exactly like a real TCU's flash would now
    hold that data), then call write_areas() again with the SAME plaintext.
    The second call must NOT send any new transferData for that area -- the
    pre-write checksum probe should report a match and skip straight past
    erase/transfer/verify -- and must report a stage message saying so."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)
    flash = Z.Zf6hpFlash(ecu)

    plain_04 = os.urandom(1200)
    flash.write_areas({0x04: plain_04})
    assert current[0x04] == plain_04, "sanity: the first (real) write should update the mock's state"
    chunks_after_first_write = len(captured[0x04])
    assert chunks_after_first_write > 0

    stages = []
    flash.write_areas({0x04: plain_04}, on_stage=stages.append)

    assert len(captured[0x04]) == chunks_after_first_write, (
        "a second write of IDENTICAL content must not send any new transferData -- "
        "the checksum pre-check should have skipped erase/transfer entirely")
    assert any("unchanged" in s.lower() and "skip" in s.lower() for s in stages), (
        "expected a stage message reporting the area was skipped as unchanged, got %r" % stages)
    assert not any("erasing" in s.lower() for s in stages), (
        "erase must not run for a skipped area")


def test_write_areas_writes_and_dynamically_verifies_when_checksum_differs():
    """No content seeded (mismatch by construction): a normal full write must
    proceed, and -- this is the actual bug fix, not just new behavior -- the
    POST-write verify must succeed for ARBITRARY plaintext, not just the one
    calibration the old hardcoded verify_params template happened to match.
    Before this fix, verify_params was fixed bytes captured from one real
    session; writing anything else would have made the post-write checksum
    check falsely report a mismatch every time (see zf6hp.py's
    _checksum_routine_params docstring)."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)
    flash = Z.Zf6hpFlash(ecu)

    plain_05 = os.urandom(2500)
    stages = []
    flash.write_areas({0x05: plain_05}, on_stage=stages.append)   # must not raise

    assert stages[-1] == "Completed successfully"
    assert not any("unchanged" in s.lower() for s in stages)
    wire_stream = b"".join(captured[0x05])
    assert zf6hp_bcb_decode(wire_stream) == plain_05
    assert current[0x05] == plain_05


def test_write_areas_force_full_bypasses_the_checksum_precheck():
    """force_full=True (the GUI's "Force full write" checkbox) must rewrite
    an area even when a checksum pre-check would have reported a match --
    seed ``current`` directly to the target content (skipping the need for
    a real prior write) so a normal call WOULD skip, then confirm
    force_full=True writes it anyway."""
    plain_04 = os.urandom(1800)
    handler, captured, current = _synthetic_write_handler(initial_content={0x04: plain_04})
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)
    flash = Z.Zf6hpFlash(ecu)

    stages = []
    flash.write_areas({0x04: plain_04}, on_stage=stages.append, force_full=True)

    assert not any("skip" in s.lower() for s in stages), (
        "force_full must bypass the checksum pre-check entirely, not just override its result")
    assert len(captured[0x04]) > 0, "force_full must still actually transfer the data"
    wire_stream = b"".join(captured[0x04])
    assert zf6hp_bcb_decode(wire_stream) == plain_04


def test_write_areas_treats_a_failed_checksum_probe_as_mismatch_and_writes_normally():
    """If the pre-write checksum probe itself fails (NAK/malformed response
    -- the real TCU's behavior here is genuinely unconfirmed, see
    zf6hp_area_checksum's docstring), write_areas must NOT abort the whole
    write: it should log the failure and proceed as if the areas differ,
    same as force_full for that one area. reject_first_checksum_probe makes
    only the PRE-write probe fail; the post-write verify (the second 0xC5
    call for this area) succeeds normally, so the write completes."""
    handler, captured, current = _synthetic_write_handler(reject_first_checksum_probe=True)
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)
    flash = Z.Zf6hpFlash(ecu)

    plain_04 = os.urandom(900)
    stages = []
    flash.write_areas({0x04: plain_04}, on_stage=stages.append)   # must not raise

    assert stages[-1] == "Completed successfully"
    wire_stream = b"".join(captured[0x04])
    assert zf6hp_bcb_decode(wire_stream) == plain_04


def test_write_image_slices_a_full_image_by_write_area_length_not_bank_size():
    """write_image() is the GUI's entry point (mirrors read_image()): it takes
    a full IMAGE_SIZE (1 MiB) buffer -- the same layout read_image() produces
    -- and must slice each area's plaintext using WRITE_AREA_LENGTH, NOT
    BANK_SIZE, since a write area's real length differs from a read area's
    (area 0x04 is BANK_SIZE-128; area 0x05 covers 3*BANK_SIZE-128, spanning
    what read treats as three separate areas -- see WRITE_AREA_LENGTH's
    docstring in zf6hp.py).

    Deliberately does NOT drive a real WRITE_AREA_LENGTH-sized payload (65408
    / 196480 bytes) through the synthetic mock end to end -- at ~1000+
    transferData round trips that takes minutes even against a mock (this
    file intentionally carries no @pytest.mark.slow, unlike
    test_flash_zf6hp.py's real-capture replays), and it would only be
    re-proving write_areas' already-covered chunking mechanics. Instead this
    patches write_areas itself to capture exactly the ``images`` dict
    write_image() computes, which is the one thing specific to write_image()
    that needs checking: the slice offsets and lengths."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)

    image = bytearray(os.urandom(Z.IMAGE_SIZE))
    # Make each write area's expected slice locally recognizable (not just
    # random-vs-random) by stamping a distinct marker run at its start.
    image[0x04 * Z.BANK_SIZE:0x04 * Z.BANK_SIZE + 8] = b"\xAA" * 8
    image[0x05 * Z.BANK_SIZE:0x05 * Z.BANK_SIZE + 8] = b"\xBB" * 8

    expected = {
        area: bytes(image[area * Z.BANK_SIZE:area * Z.BANK_SIZE + length])
        for area, length in Z.WRITE_AREA_LENGTH.items()
    }

    flash = Z.Zf6hpFlash(ecu)
    calls = []

    def spy_write_areas(images, on_percent=None, on_stage=None, is_stop=None, force_full=False):
        calls.append((images, on_percent, on_stage, is_stop, force_full))

    flash.write_areas = spy_write_areas
    flash.write_image(bytes(image), on_percent="p", on_stage="s", is_stop="i", force_full=True)

    assert len(calls) == 1, "write_image must call write_areas exactly once"
    images, on_percent, on_stage, is_stop, force_full = calls[0]
    assert (on_percent, on_stage, is_stop, force_full) == ("p", "s", "i", True), (
        "write_image must pass on_percent/on_stage/is_stop/force_full through to write_areas")
    assert set(images) == set(Z.WRITE_AREA_LENGTH)
    for area, plain in expected.items():
        assert len(images[area]) == Z.WRITE_AREA_LENGTH[area], (
            "area 0x%02X: write_image sliced %d bytes, expected "
            "WRITE_AREA_LENGTH=%d" % (area, len(images[area]), Z.WRITE_AREA_LENGTH[area]))
        assert images[area] == plain, (
            "area 0x%02X: write_image sliced the wrong bytes (expected "
            "WRITE_AREA_LENGTH=%d bytes starting at offset 0x%X)"
            % (area, Z.WRITE_AREA_LENGTH[area], area * Z.BANK_SIZE))


def test_write_image_end_to_end_with_small_synthetic_lengths():
    """A small, fast end-to-end check that write_image() -> write_areas() ->
    the wire actually works together (not just that the slice is correct in
    isolation, as the test above checks) -- exercised against a shrunk
    WRITE_AREA_LENGTH so it stays fast, restored afterward so it can't leak
    into other tests. Only area 0x04 is used (one area is enough to prove
    the plumbing; doubling it up with 0x05 only doubles the mock traffic for
    no additional coverage)."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)

    orig_lengths = dict(Z.WRITE_AREA_LENGTH)
    small_len = 600   # a few transferData chunks, not hundreds
    Z.WRITE_AREA_LENGTH[0x04] = small_len
    try:
        image = bytearray(os.urandom(Z.IMAGE_SIZE))
        expected = bytes(image[0x04 * Z.BANK_SIZE:0x04 * Z.BANK_SIZE + small_len])
        flash = Z.Zf6hpFlash(ecu)
        flash.write_image(bytes(image), areas=(0x04,))
        wire_stream = b"".join(captured[0x04])
        assert zf6hp_bcb_decode(wire_stream) == expected
    finally:
        Z.WRITE_AREA_LENGTH.clear()
        Z.WRITE_AREA_LENGTH.update(orig_lengths)


def test_write_image_rejects_a_short_image():
    """A buffer shorter than IMAGE_SIZE can't be sliced by area*BANK_SIZE
    offsets safely -- write_image() must reject it up front rather than
    silently writing truncated/wrong-offset garbage to the TCU."""
    handler, captured, current = _synthetic_write_handler()
    ecu = MockTp20Ecu(handler, dest=Z.DEST, ecu_tx=0x300, ecu_rx=0x760)
    flash = Z.Zf6hpFlash(ecu)
    try:
        flash.write_image(os.urandom(Z.IMAGE_SIZE - 1))
    except Z.KLineError:
        pass
    else:
        raise AssertionError("write_image should reject an under-length image")
