# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 fast-logging bring-up -- high baud via conditional-baud detour (v0.8).

The firmware re-asserts S0BG=0x2F (10400) at every KWP response-TX state, which is
why earlier attempts to raise the baud never stuck. v0.8 patches those response-TX
baud assertions to be *flag-gated*: a resident detour at each site reads a RAM flag
word (0xD086) and uses S0BG=0x0C (38400) when it equals the magic 0xA55A, else the
stock 0x2F (10400). Our 0x90 hook arms that flag (and builds the proven v0.6
4-channel frame). Because the flag is clear during the 5-baud wake and key-byte
exchange, the handshake and all normal comms stay at 10400 -- only responses *after*
our 0x90 fires go to 38400. Engine stays in the extended session throughout (no
programming handover, unlike the 0x85 flashing path which is engine-off).

Sequence: wake KW1281 -> KWP2000 (10400) -> 0x89 (extended) -> send 10 90 at 10400
(ECU arms the flag; its response comes back at 38400) -> switch to 38400, read the
4-channel frame, verify the 0xA55A marker -> send a 2nd 10 90 at 38400 to confirm
the link is now continuously fast.

Requires the v0.8 flash (checkpoint 6de30a2055). 125000 is the same patch rebuilt
with S0BG target 0x03.

Selected in the logger as the "EDC15-fast" ECU.
"""
import struct
import time

from . import kwp as _kwp
from .transport import KLineError, KLineTimeout

MARK = bytes([0x50, 0x90])
SVC_START_DIAG = 0x10
SUBFN_FASTLOG = 0x90
SUBFN_STORE = 0x93          # v2.6: store up to STORED_MAX addresses on the ECU and read them
SUBFN_LIST = 0x91           # v2.6: read the stored list (10 91 <s0bg>, 3-byte request)
STORED_RAM = 0xD1A0         # v2.7 list (GN): count word + STORED_MAX address words; read zero/stable at key-on
FLAG_RAM = 0xD086           # the GN's arm flag; every other build names its own (fasthook.HookBuild)
STORED_MAX = 12
# The stored list writes ECU RAM. On the GN the first slot (0xD372, v2.6) turned out to be
# live data; v2.7 uses 0xD1A0, nominated by edc15.freeram and read all-zero on the car. Each
# build names its own RAM (HookBuild; the patcher, edc15.fastpatch15, picks and checks it --
# the logger does not probe). Needs flash v2.7: on v2.6 the list would hit 0xD372 -- keep it
# off there.
STORED_LIST_ENABLED = True


def flag_addr(link):
    """RAM address the connected build's arm flag lives at (a harmless word to read)."""
    b = getattr(link, "_fl_build", None)
    return b.flag if b is not None else FLAG_RAM


def stored_addr(link):
    """RAM address of the connected build's stored list (its count word)."""
    b = getattr(link, "_fl_build", None)
    return b.stored if b is not None else STORED_RAM
SUBFN_GROUP = 0x91          # v1.3: fast measuring-group read (10 91 <s0bg> <group>)
GROUP_TITLE = 0xE7          # KW1281 measuring-block response title (BT_GROUP_READ_RESPONSE)
MAGIC_ARM_GROUP = 0xFB      # v1.8: KW1281 group number that arms the de-ACK burst (29 FB)
MARKER = 0xA55A
SAMPLE_ADDRS = [0xD090, 0xD092, 0xD094]
#: v1.2 raw-RAM mode: addresses the client asks the ECU to read+stream (test set).
RAM_ADDRS = [0xD090, 0xD092, 0xD094, 0xD096]
#: Default fast baud the client requests. With the v1.1 flash the client CHOOSES the
#: baud: it sends 10 90 <s0bg>, the hook stores <s0bg> as the target, and the responder
#: transmits at it. So this is just a default -- any baud the UART divisor supports works.
FAST_BAUD = 125000


def _s0bg_for(baud):
    """EDC15 S0BG reload for a baud: baud = 500000/(S0BG+1) (C167 @16MHz).
    e.g. 125000->0x03, 38400->0x0C, 10400->0x2F."""
    return max(0, min(0xFF, round(500000.0 / baud) - 1))


class BringupResult:
    def __init__(self):
        self.steps = []
        self.cap1 = b""        # first 0x90 response, read at the fast baud
        self.cap2 = b""        # second 0x90 response (continuous check), at fast baud
        self.words = None
        self.baud = FAST_BAUD
        self.marker_ok = False
        self.hook_ok = False
        self.continuous_ok = False
        self.ok = False

    def note(self, msg):
        self.steps.append(msg)

    def as_text(self):
        import datetime
        out = ["EDC15 high-baud logging test (v0.8 conditional-baud detour)",
               "run: %s" % datetime.datetime.now().isoformat(timespec="seconds"), ""]
        out += self.steps
        out += ["",
                "%d capture      : %s" % (self.baud, self.cap1.hex(" ") or "(none)"),
                "10400 fallback  : %s" % (self.cap2.hex(" ") or "(none)")]
        if self.words is not None:
            out.append("")
            out.append("decoded %d words at %d baud:" % (len(self.words), self.baud))
            if self.words:
                out.append("  sample 0 (marker)   : 0x%04X  (expect 0x%04X -> %s)"
                           % (self.words[0], MARKER,
                              "OK" if self.words[0] == MARKER else "CORRUPT"))
            for n, (a, v) in enumerate(zip(SAMPLE_ADDRS, self.words[1:]), start=1):
                out.append("  sample %d  RAM[0x%04X] : 0x%04X" % (n, a, v))
        else:
            out.append("decoded frame       : (no clean 50 90 frame found)")
        out += ["",
                "hook responded    : %s" % self.hook_ok,
                "marker clean      : %s" % self.marker_ok,
                "VERDICT           : %s" % ("HIGH-BAUD OK" if self.ok else
                    ("FRAME AT 10400 (baud change did not take)" if self.hook_ok else "SEE LOG"))]
        return "\n".join(out)


def _drain(link, window, maxn=256):
    out = bytearray()
    link.read_timeout = window
    while len(out) < maxn:
        try:
            out.append(link.read_byte())
        except (KLineTimeout, KLineError, OSError):
            break
    return bytes(out)


def _read_frame(link, nwords, window):
    """Read a 50 90 reply and return as soon as it holds ``50 90`` + 2*nwords
    data bytes -- no trailing timeout wait (that wait, ~window per read, was the
    throughput killer). ``window`` is only the safety ceiling."""
    import time as _t
    deadline = _t.monotonic() + window
    link.read_timeout = 0.02
    buf = bytearray()
    want = 2 * nwords
    while _t.monotonic() < deadline:
        try:
            buf.append(link.read_byte())
        except (KLineTimeout, KLineError, OSError):
            i = buf.find(MARK)
            if i >= 0 and len(buf) - (i + 2) >= want:
                break
            continue
        i = buf.find(MARK)
        if i >= 0 and len(buf) - (i + 2) >= want:
            break
        j = buf.find(b"\x7f\x10")
        if i < 0 and j >= 0 and len(buf) - j >= 3:
            break                    # a negative response: no data frame is coming
        # ``83 10 01 7F <sub> 13``: the ECU lost the request's FIRST byte (seen on
        # the serial path, ~12 % of requests) and answered the shifted frame with
        # "incorrect length".  Fail now instead of waiting out the window.
        if i < 0 and _nrc_shifted(buf):
            break
    return bytes(buf)


def _nrc_shifted(buf):
    for sub in (SUBFN_FASTLOG, SUBFN_LIST, SUBFN_STORE):
        k = buf.find(bytes([0x7F, sub]))
        if 0 <= k <= len(buf) - 3:
            return True
    return False


def _decode_frame(buf):
    i = buf.find(MARK)
    if i < 0:
        return None
    body = buf[i + 2:]
    n = len(body) // 2
    return [body[2 * k] | (body[2 * k + 1] << 8) for k in range(n)] if n else None


# The ECU ignores a request that starts less than ~56 ms after its previous
# reply ended (P3): a 2026-10-31 FTDI sniff shows every request sent 50-54 ms
# after a reply unanswered, ~50 % at 54-56 ms and none above 58 ms -- the J2534
# path always leaves ~58 ms.  Sleep granularity (15 ms on Windows) made the
# serial path land below the limit about a quarter of the time.
P3_MIN_S = 0.058


def _set_baud(link, baud):
    """``link.set_baud`` unless the port is already at ``baud``.  In steady
    streaming the send and reply rates are the same, and the D2XX setter calls
    FT_SetBaudRate every time -- reprogramming the divisor twice per request.
    A J2534 channel changes rate ~8 times per whole session; the serial path did
    it twice per request, and ~12 % of its requests reach the ECU minus their
    first byte (sniff 2026-10-31).  Only change the rate when it differs."""
    cur = getattr(getattr(link, "port", None), "baudrate", None)
    if cur is not None and int(cur) == int(baud):
        return
    link.set_baud(baud)


def _p3_wait(link, p3=None):
    """Block until ``p3`` (default P3_MIN_S) has passed since the last reply
    byte; coarse sleep, then spin for the last ~2 ms."""
    last = getattr(link, "_fl_last_rx", None)
    if last is None:
        return
    end = last + (P3_MIN_S if p3 is None else p3)
    while True:
        rem = end - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.004:
            time.sleep(rem - 0.003)
        # else spin


def _fast_cmd(link, kwp, sub, params, fast_baud, window=0.5, send_baud=10400,
              nwords=None):
    """Send ``10 <sub> <s0bg> <params>`` at ``send_baud``, switch to
    ``fast_baud`` for the reply, and return (words, raw) from the 50 90 frame.

    ``send_baud`` matters for repeated reads: the hook's first reply goes out at
    ``fast_baud`` and leaves the ECU's S0BG there (the baud flag stays armed and
    nothing restores it), so every request AFTER the first must be sent at
    ``fast_baud`` too -- the ECU is no longer listening at 10400. The caller
    passes ``send_baud=fast_baud`` for those."""
    s0bg = _s0bg_for(fast_baud)
    if nwords is None:
        nwords = params[0] if params else 0      # 0x90: params = [n] + addr words
    _set_baud(link, send_baud)                    # slow driver calls first, ...
    link.clear_rx()
    _p3_wait(link)                                # ... then wait out P3 right before TX
    # Kwp2000.send() sleeps P3 itself before every frame; _p3_wait above has
    # already guaranteed the gap measured from the last reply byte, so that
    # second sleep only stacked ~25 ms onto every cycle (sniff 2026-10-31 19:45:
    # 84 ms reply->request vs J2534's 58, cycle 180 ms vs 160).
    p3_saved = kwp.P3
    if getattr(link, "_fl_last_rx", None) is None:
        time.sleep(p3_saved)                      # first request after another service
    t_req = time.perf_counter()                   # (the diagnostics time send + reply)
    kwp.P3 = 0.0
    try:
        kwp.send(SVC_START_DIAG, bytes([sub, s0bg]) + bytes(params))
    finally:
        kwp.P3 = p3_saved
    _set_baud(link, fast_baud)
    cap = _read_frame(link, nwords, window)
    link._fl_last_rx = time.perf_counter()
    w = _decode_frame(cap)
    if w is None and send_baud != fast_baud:
        # one-shot fallback: the reply may have come at 10400 (baud never changed)
        link.set_baud(10400)
        cap2 = _read_frame(link, nwords, window)
        w = _decode_frame(cap2)
        cap = cap + cap2
    _diag_request(link, sub, w, cap, nwords, time.perf_counter() - t_req)
    return w, cap


class FastGateError(KLineError):
    """The fast path must not run on this ECU (identity mismatch); the message
    is shown to the user as is."""


def _wake_kwp(link):
    """One 5-baud KWP wake at 10400; the protocol version, or 0 on no/wrong answer."""
    try:
        return link.wake_up(0x01, even_parity=False, tries=1)
    except KLineError:
        return 0


#: The NRC the stock ``10 89`` handler answers with when the engine is turning.
NRC_NOT_WITH_ENGINE = 0x12


def _extended_session(kwp, log):
    """``10 89``, tolerating the engine-running refusal; True when the ECU accepted it.

    The stock 0x89 handler (038906012GN 0xB39FA) loads the engine-state word it copied
    at entry (0xF964, from 0xF948 -- sampled everywhere as the engine speed) and takes
    the NRC 0x12 sender when it is not zero; only with that word at zero does it set the
    two session flags (0xC419, 0xC343) and answer.  So with the engine running the KWP
    wake works (the ECU answers) but this one request is refused.  The fast-logging
    sub-functions are dispatched on the sub-function byte alone, before and apart from
    that handler, so the raw-RAM reader needs no extended session: go on without it.
    Anything but that one refusal is still an error."""
    log("EDC15-fast: KWP2000 wake answered; sending 10 89 (extended session)...")
    try:
        kwp.send_receive(SVC_START_DIAG, b"\x89")
        log("EDC15-fast: 10 89 accepted.")
        return True
    except _kwp.NegativeResponse as ex:
        if ex.service != SVC_START_DIAG or ex.nrc != NRC_NOT_WITH_ENGINE:
            log("EDC15-fast: 10 89 refused with NRC 0x%02X (service 0x%02X)."
                % (ex.nrc, ex.service))
            raise
    log("EDC15-fast: the ECU refuses the extended session (10 89 -> NRC 0x12), as it "
        "does while the engine is running; going on without it -- the raw-RAM reader "
        "does not need it.")
    return False


def _kwp_session(link, fast_baud, log):
    """Wake the ECU into a KWP2000 session and open the extended diagnostic
    session; returns the Kwp2000 object.

    When the wake goes unanswered the likely cause is a fast-baud state left by
    an earlier session that was not closed (the ECU then answers the wake at that
    stale rate; the KW1281 wake that precedes this is unaffected, which is why
    the standard logger still connects). So listen for the answer at the fast
    rates too, and if one is heard, finish the init there and send the closing
    read that names the stock rate -- the ECU is then back to normal."""
    ver = _wake_kwp(link)
    if ver >= 2000:
        kwp = _kwp.Kwp2000(link, 0x01, log)
        _extended_session(kwp, log)
        return kwp
    log("EDC15-fast: no KWP answer at 10400 (version %s) -- looking for a stale "
        "fast-baud state..." % (ver or "none"))
    for rb in dict.fromkeys((fast_baud, 38400, 125000)):
        link.set_baud(10400); link.clear_rx()
        link.wake_reply_baud = rb
        try:
            time.sleep(1.0)
            ver = _wake_kwp(link)
        finally:
            link.wake_reply_baud = None
        if ver >= 2000:
            log("EDC15-fast: ECU answered the wake at %d baud (stale state); "
                "restoring 10400." % rb)
            kwp = _kwp.Kwp2000(link, 0x01, log)
            try:
                _extended_session(kwp, log)
            except (KLineError, KLineTimeout, OSError):
                pass
            if not fast_stop(link, kwp, rb, log):
                break
            time.sleep(0.5)
            return kwp
    # the ECU may simply be waiting to be woken once more at the normal rate
    link.set_baud(10400); link.clear_rx()
    time.sleep(1.0)
    ver = _wake_kwp(link)
    if ver >= 2000:
        kwp = _kwp.Kwp2000(link, 0x01, log)
        _extended_session(kwp, log)
        return kwp
    raise KLineError(
        "could not reach a KWP2000 session (KW %s). The ECU may still be set to "
        "a fast rate by an earlier session: switch the ignition off and on, or "
        "set Speed to Low for standard logging." % (ver or "none"))


def fast_stream_connect(link, fast_baud, log=None, is_stop=None, expect_part=None,
                        build=None):
    """Bring up an EDC15-fast session: KW1281 9600 wake, then the KWP2000
    extended session (0x89), exactly as the first connect does. Returns the
    Kwp2000 session, or None if stopped. The first raw-RAM read then primes the
    fast baud -- pass ``first=True`` to the first :func:`fast_read`.

    NOTE: a clean fast-logging session must leave the ECU in its stock baud
    state so the *next* connect is identical to this one. That reversion is the
    ECU's job (the arm flag must be cleared at session end / next init); the
    client does not chase a still-fast ECU here.

    ``build`` is the :class:`~ovtsuite.edc15.fasthook.HookBuild` the open binary
    carries: its RAM (flag, stored list) is what the session uses.  No RAM is probed
    here -- the patcher picks it and checks it on the ECU (edc15.fastprobe15)."""
    log = log or (lambda m: None)
    link._fl_build = build
    link._fl_log = log
    link.set_baud(9600); link.clear_rx()
    link.wake_up(0x01, even_parity=False)
    kw0 = _kwp.Kw1281(link, log)
    blocks = kw0.connect()
    try:
        from .diag import kw1281_identity
        link._fl_ident = kw1281_identity(blocks)[0]
    except Exception:                                # noqa: BLE001 -- identity is advisory
        link._fl_ident = ""
    if link._fl_ident:
        log("EDC15-fast: ECU identifies as %s" % link._fl_ident)
    if expect_part:
        from ..edc15 import fasthook
        if fasthook.ident_matches(link._fl_ident, expect_part) is False:
            try:
                kw0.end_communication()
            except Exception:                        # noqa: BLE001
                pass
            raise FastGateError(
                "The ECU identifies as \"%s\" but the open binary is %s. Open the "
                "binary that is flashed in this ECU, or set Speed to Low for "
                "standard logging." % (link._fl_ident, expect_part))
    # Read one measuring group over KW1281 before opening the KWP session. The
    # on-car validate did exactly this and its 0x90 reads returned real values;
    # the stream (connect + end only) got empty reads back. The group read leaves
    # the firmware's measuring/comms state set up -- the CED2 response-buffer
    # pointer the 0x90 hook builds its reply into -- so without it the hook has
    # no valid buffer to fill and the reply comes back empty.
    try:
        kw0.send_block([0x29, 0x01]); kw0.receive_block()
    except (KLineError, KLineTimeout, OSError) as ex:
        log("EDC15-fast: preamble group read skipped (%s)" % ex)
    kw0.end_communication()
    time.sleep(1.0)                      # settle before the KWP re-init
    if is_stop and is_stop():
        return None
    link.set_baud(10400); link.clear_rx()
    log("EDC15-fast: KW1281 session closed; waking the ECU into KWP2000 at 10400...")
    kwp = _kwp_session(link, fast_baud, log)
    # Timing. On the bench trace one single-word read cost ~180 ms: P3 55 ms +
    # ~70 ms byte-wise send (10 bytes x 7 ms) + ~40 ms for the ECU's answer.
    # A whole frame in one write gets NO answer (the ECU's KWP receiver needs
    # inter-byte spacing), and P3 = 10 ms made the first read go unanswered, so
    # neither is assumed: find the smallest P4 and P3 this ECU+cable accepts,
    # then keep a margin. Failures here only cost a 0.2 s read window each.
    kwp.fast = False
    fl = flag_addr(link)
    if fast_read(link, kwp, [fl], fast_baud, first=True) is None and \
       fast_read(link, kwp, [fl], fast_baud, first=False) is None:
        why = explain_no_answer(link)
        log("EDC15-fast: first raw-RAM read of 0x%04X failed at %d baud -- %s"
            % (fl, fast_baud, why))
        stock = _stock_rate_reply(link, kwp, fl, fast_baud)
        if stock is None:
            raise KLineError("EDC15-fast: the ECU did not answer the first raw-RAM read "
                             "of 0x%04X (%s)" % (fl, why))
        # The reader is there and answers; only the switch to the fast rate does not
        # take on this ECU. A read costs ~150 ms whatever the baud (the ECU's own
        # turnaround and the 58 ms gap dominate), so log at the stock rate.
        log("EDC15-fast: the reader answers at 10400 but not at %d: this ECU does not "
            "follow that baud change." % fast_baud)
        fast_baud = _find_rate(link, kwp, fl, fast_baud, log)
        log("EDC15-fast: logging at %d baud." % fast_baud)
    link._fl_baud = fast_baud

    def _ok(n=3):
        return all(fast_read(link, kwp, [fl], fast_baud) is not None
                   for _ in range(n))

    kwp.P3 = 0.055
    p4_ok = 0.005
    probe = []
    for p4 in (0.0005, 0.001, 0.002, 0.003):
        kwp.P4 = p4
        good = _ok()
        probe.append("P4 %.1f %s" % (p4 * 1000, "ok" if good else "x"))
        if good:
            p4_ok = p4
            break
    kwp.P4 = max(0.001, p4_ok * 1.5) if p4_ok < 0.005 else 0.005
    p3_ok = 0.055
    for p3 in (0.015, 0.025, 0.035, 0.045):
        kwp.P3 = p3
        good = _ok()
        probe.append("P3 %d %s" % (round(p3 * 1000), "ok" if good else "x"))
        if good:
            p3_ok = p3
            break
    kwp.P3 = p3_ok + 0.01 if p3_ok < 0.055 else 0.055
    link._fl_timing = "P3 %d ms, P4 %.1f ms [%s]" % (
        round(kwp.P3 * 1000), kwp.P4 * 1000, " ".join(probe))
    log("EDC15-fast: request timing %s" % link._fl_timing)
    link._fl_list = None; link._fl_stored_fail = 0; link._fl_slot_checked = None
    # (_fl_last_rx stays at the last timing read's reply: the first request after the
    # connect must wait out the same 58 ms as every other -- reset to None it waited only
    # the shorter P3 the timing probe found, and the A6 ignored that request)
    link._fl_stored_ok = None if STORED_LIST_ENABLED else False
    link._fl_stats = None
    link._fl_diag = None                 # the session's own diagnostics start with its first row
    return kwp


def fast_read(link, kwp, addrs, fast_baud, first=False, window=0.15):
    """Read ``addrs`` in one 0x90 burst; return ``{addr: word}``, or None on a
    failed/short read. ``first=True`` sends at 10400 (the reply leaves the ECU
    at the fast baud); every later read sends at ``fast_baud``, because the ECU
    is now listening there."""
    if not addrs:
        return {}
    sb = 10400 if first else fast_baud
    params = bytes([len(addrs)]) + b"".join(struct.pack("<H", a) for a in addrs)
    t0 = time.perf_counter()
    w, _ = _fast_cmd(link, kwp, SUBFN_FASTLOG, params, fast_baud, window,
                     send_baud=sb)
    dt = time.perf_counter() - t0
    ok = bool(w) and len(w) >= len(addrs)
    _note(link, dt, ok)
    if not ok:
        link._fl_nrc = _nrc_of(_)
        link._fl_cap = bytes(_ or b"")
        return None
    link._fl_nrc = None
    link._fl_cap = b""
    return {a: w[i] for i, a in enumerate(addrs)}


def _stock_rate_reply(link, kwp, flag, fast_baud):
    """After a first read that drew no clean reply: ask for the same word with the
    reply rate set to the STOCK 10400 (s0bg 0x2F). A clean frame means the reader
    works and the fast rate is what failed. Tried at 10400, then at ``fast_baud`` in
    case the ECU's receiver did switch. Returns the word, or None. Leaves the line at
    10400 and the ECU with target = stock, i.e. as after power-up for the UART."""
    params = bytes([1]) + struct.pack("<H", flag)
    for send in dict.fromkeys((10400, fast_baud)):
        try:
            link._fl_last_rx = None
            w, _cap = _fast_cmd(link, kwp, SUBFN_FASTLOG, params, 10400, window=0.4,
                                send_baud=send)
        except (KLineError, KLineTimeout, OSError):
            continue
        if w:
            return w[0]
    return None


def _find_rate(link, kwp, flag, wanted, log):
    """The highest rate below ``wanted`` this ECU follows, else the stock 10400.
    The ECU is at 10400 on entry (a clean stock-rate reply has just been heard); a
    rate that draws no clean reply is followed by another stock-rate read, which
    puts the UART back, so every try starts from a known state."""
    for b in (62500, 38400, 31250, 19200):
        if b >= wanted:
            continue
        if fast_read(link, kwp, [flag], b, first=True) is not None:
            log("EDC15-fast: the ECU follows %d baud." % b)
            return b
        log("EDC15-fast: %d baud: no clean reply (%s)" % (b, explain_no_answer(link)))
        if _stock_rate_reply(link, kwp, flag, b) is None:
            raise KLineError("EDC15-fast: the ECU stopped answering while the baud rates "
                             "were tried; switch the ignition off and on")
    return 10400


def explain_no_answer(link):
    """Why the last raw-RAM read failed, from the bytes the ECU did send: the
    first read of a session tells a build that is not in the ECU (NRC 0x12), one
    that is there but breaks the request path (silence) and a bad reply."""
    cap = getattr(link, "_fl_cap", b"") or b""
    nrc = getattr(link, "_fl_nrc", None)
    if nrc in (0x11, 0x12):
        return ("it answered 'sub-function not supported' (NRC 0x%02X): the ECU is not "
                "running this fast-logging patch -- the patched image was not flashed, "
                "or the hook's flash sector did not take" % nrc)
    if nrc is not None:
        return "it answered a negative response (NRC 0x%02X): %s" % (nrc, cap.hex(" "))
    if not cap:
        return ("it sent nothing, at the fast rate or at 10400: the request reached no "
                "reader or the reader hung (a patch whose RAM or hook does not suit this "
                "ECU), or the reply came at a rate this client is not listening at")
    return "it sent bytes that are not a raw-RAM reply: %s" % cap.hex(" ")


def _note(link, dt, ok):
    st = getattr(link, "_fl_stats", None)
    if st is None:
        st = link._fl_stats = {"n": 0, "bad": 0, "t": 0.0, "min": 9.9}
    st["n"] += 1; st["t"] += dt
    if ok:
        st["min"] = min(st["min"], dt)
    else:
        st["bad"] += 1


#: A row that took longer than this is "slow". A normal 12-address row is ~0.2 s; the
#: slow ones seen on the 038906012CP (2026-11-01) took ~0.5 s, in runs of up to 28 s.
DIAG_SLOW_ROW = 0.35


def _diag(link):
    """The session's read diagnostics (diagnostic only: nothing here changes how a
    read is made). ``req`` counts requests by sub-function and outcome, with the time
    each took; ``events`` are the requests of the row in progress; ``run`` is the
    current run of slow rows."""
    d = getattr(link, "_fl_diag", None)
    if d is None:
        d = link._fl_diag = {"req": {}, "rows": 0, "slow": 0, "runs": 0, "longest": 0,
                             "run": None, "events": []}
    return d


def _outcome(w, cap, nwords):
    """How a request ended: ok, NRC 0xNN, short (a frame with too few words), silent
    (nothing received) or junk (bytes that are no reply)."""
    if w and len(w) >= (nwords or 1):
        return "ok"
    nrc = _nrc_of(cap)
    if nrc is not None:
        return "NRC 0x%02X" % nrc
    if w:
        return "short"
    return "silent" if not cap else "junk"


def _diag_request(link, sub, w, cap, nwords, dt):
    try:
        d = _diag(link)
        out = _outcome(w, cap, nwords)
        c = d["req"].setdefault("%02X" % sub, {}).setdefault(out, [0, 0.0])
        c[0] += 1
        c[1] += dt
        d["events"].append((sub, out, dt))
        del d["events"][:-64]
    except Exception:                                # noqa: BLE001 -- never break a read
        pass


def _events_text(events):
    return ", ".join("%02X %s %.0f ms" % (s, o, 1000 * t) for s, o, t in events) or "no request"


def _cause(events):
    """What went wrong in a slow row, from its requests."""
    bad = ["%02X %s" % (s, o) for s, o, _t in events if o != "ok"]
    if any(s == SUBFN_STORE for s, _o, _t in events):
        bad.append("list stored again")
    return " + ".join(bad) or "no failed request"


def _end_run(link, d):
    run, d["run"] = d["run"], None
    if run is None:
        return
    d["runs"] += 1
    d["longest"] = max(d["longest"], run["rows"])
    if run["rows"] >= 2:
        _say(link, "slow rows: %d in a row, %.1f s (%.0f ms each) -- %s. First row: %s." % (
            run["rows"], run["t"], 1000 * run["t"] / run["rows"],
            ", ".join("%s x%d" % kv for kv in sorted(run["causes"].items(),
                                                     key=lambda kv: -kv[1])),
            run["first"]))


def _diag_row(link, dt):
    try:
        d = _diag(link)
        d["rows"] += 1
        if dt > DIAG_SLOW_ROW:
            d["slow"] += 1
            run = d["run"]
            if run is None:
                run = d["run"] = {"rows": 0, "t": 0.0, "causes": {},
                                  "first": "%.0f ms: %s" % (1000 * dt, _events_text(d["events"]))}
            run["rows"] += 1
            run["t"] += dt
            k = _cause(d["events"])
            run["causes"][k] = run["causes"].get(k, 0) + 1
        elif d["run"] is not None:
            _end_run(link, d)
    except Exception:                                # noqa: BLE001
        pass


def fast_diag_summary(link, log):
    """The session's requests by sub-function and outcome, and its slow rows, as log
    lines; nothing when the session read nothing (a probe, a failed connect)."""
    d = getattr(link, "_fl_diag", None)
    if not d or not d["rows"]:
        return
    try:
        _end_run(link, d)
        log("EDC15-fast: %d row(s), %d slow (over %.0f ms) in %d run(s), longest %d row(s)." % (
            d["rows"], d["slow"], 1000 * DIAG_SLOW_ROW, d["runs"], d["longest"]))
        for sub in sorted(d["req"]):
            log("EDC15-fast: 10 %s requests: %s" % (sub, ", ".join(
                "%s %d (avg %.0f ms)" % (o, n, 1000 * t / n)
                for o, (n, t) in sorted(d["req"][sub].items(), key=lambda kv: -kv[1][0]))))
    except Exception:                                # noqa: BLE001
        pass


def fast_read_stored(link, kwp, addrs, fast_baud, window=0.15):
    """v2.6: read ``addrs`` (1..STORED_MAX) through the ECU-side stored list.

    The first call for a given address tuple sends ``10 93 <s0bg> <n> <addrs>``
    (stores the list AND returns its values); every call after that for the same
    tuple is just ``10 91 <s0bg>``. A different tuple simply re-stores. Returns
    ``{addr: word}`` or None; any failure forgets the cached list so the next
    call re-stores (the ECU may have dropped it, e.g. a session restart)."""
    n = len(addrs)
    t0 = time.perf_counter()
    if getattr(link, "_fl_list", None) == tuple(addrs):
        # Pad to a 6-byte body: the stock 0x10 handler treats a 3-byte body as
        # "<sub> <baud>" and rejects an unknown sub itself (NRC 0x12) without
        # ever reaching the hook; lengths >= 6 are what v2.5 passes through.
        sub, params = SUBFN_LIST, b"\x00\x00\x00"
    else:
        sub = SUBFN_STORE
        params = bytes([n]) + b"".join(struct.pack("<H", a) for a in addrs)
    if sub == SUBFN_LIST:
        # The reply is complete ~35 ms ECU latency + ~3 ms/byte after the request;
        # waiting the generic 150 ms ceiling for a request the ECU swallowed made
        # every miss cost ~310 ms (sniff 2026-10-31: ~28 % of 0x91 reads got no
        # reply at all, roughly every 0.5-1.6 s).  Time out shortly after the
        # reply should have ended instead.
        window = min(window, 0.05 + 0.003 * (4 + 2 * n) + 0.04)
    w, cap = _fast_cmd(link, kwp, sub, params, fast_baud, window,
                       send_baud=fast_baud, nwords=n)
    ok = bool(w) and len(w) >= n
    _note(link, time.perf_counter() - t0, ok)
    if not ok:
        link._fl_nrc = _nrc_of(cap)
        # A silent miss does not mean the ECU forgot the list: retry the short
        # 0x91 once before falling back to a full 0x93 re-store.
        if sub == SUBFN_LIST and MARK not in cap and link._fl_nrc is None and not getattr(link, "_fl_silent", False):
            link._fl_silent = True
            return None
        link._fl_silent = False
        link._fl_list = None
        return None
    link._fl_silent = False
    link._fl_nrc = None
    link._fl_list = tuple(addrs)
    return {a: w[i] for i, a in enumerate(addrs)}


def _say(link, msg):
    log = getattr(link, "_fl_log", None)
    if log is not None:
        log("EDC15-fast: " + msg)


def _try_stored(link, kwp, addrs, fast_baud, window):
    """Stored-list read of the first STORED_MAX addresses, with retries.
    Returns the {addr: word} map, or None to make the caller use plain 0x90.
    The feature is switched off for the session on a 'sub-function not
    supported' NRC (flash older than v2.6) or after three failed rows in a row."""
    if not STORED_LIST_ENABLED or getattr(link, "_fl_stored_ok", None) is False:
        return None
    part = list(addrs[:STORED_MAX])
    for _try in range(3):
        got = fast_read_stored(link, kwp, part, fast_baud, window=window)
        if got is not None:
            if getattr(link, "_fl_slot_checked", None) is None:
                # first store of the session: the flash must have put the list
                # where this client expects it (the build's list RAM). A v2.6 flash
                # stores at 0xD372 instead -- stop using the feature there.
                sa = stored_addr(link)
                # (a transient NRC 0x13 or a lost reply is not "the list is elsewhere":
                # ask again; the A6's first check drew a 0x13 and the list was dropped)
                chk = None
                for _ask in range(3):
                    chk = fast_read(link, kwp, [sa], fast_baud, window=window)
                    if chk is not None:
                        break
                link._fl_slot_checked = bool(chk) and chk.get(sa) == len(part)
                if not link._fl_slot_checked:
                    link._fl_stored_ok = False
                    link._fl_list = None
                    _say(link, "the stored list's count is not at 0x%04X after the store: "
                               "no stored list (longer requests)" % sa)
                    return got           # this row's values are still valid
            link._fl_stored_ok = True
            link._fl_stored_fail = 0
            return got
        if getattr(link, "_fl_nrc", None) in (0x11, 0x12):
            link._fl_stored_ok = False
            _say(link, "the ECU refused the stored address list (NRC 0x%02X): the list RAM "
                       "0x%04X does not keep the count the patch writes there, or the patch "
                       "is older than v2.6. Logging with full requests (about 20 ms slower "
                       "per row); rebuild the patch with another list RAM"
                 % (link._fl_nrc, stored_addr(link)))
            return None
        if getattr(link, "_fl_list", None) is None:
            time.sleep(0.02)             # (a plain retry of a silent 0x91 needs no pause)
    link._fl_stored_fail = getattr(link, "_fl_stored_fail", 0) + 1
    if link._fl_stored_fail >= 3:
        link._fl_stored_ok = False
    return None


def fast_stop(link, kwp, fast_baud, log=None):
    """Hand the ECU's UART back at the stock baud before the session is dropped.

    The ECU has no StopCommunication (it answers ``7F 82 11``), so a session just
    ends by timeout, and the fast-baud state this feature set up survives it: the
    conditional-baud sites keep asserting the client's target S0BG (RAM 0xD08C)
    while the flag 0xD086 stays armed, and nothing in the firmware clears either.
    The client chooses that target per request, so the last request names the
    STOCK value: one raw-RAM read sent at the fast baud with s0bg = 10400's, whose
    reply comes back at 10400. From then on every re-asserted S0BG is the stock
    one, i.e. the ECU is as after power-up as far as the UART goes.

    This request is the only thing that un-sticks the ECU, so it is confirmed
    (the 50 90 reply must arrive) and retried -- a single lost request left the
    ECU answering its next KWP wake at the stale rate, i.e. not at all (sniff
    2026-10-31 22:40: every fast reconnect failed after one 38400 session that
    ended without a stop reply, while the standard KW1281 path kept working).
    Returns True when the ECU confirmed; never raises; leaves the line at 10400."""
    log = log or (lambda m: None)
    fast_diag_summary(link, log)
    ok = False
    for attempt in range(3):
        try:
            # two tries where the ECU listens (the fast rate), then one at 10400
            # in case it is already stock
            w, _ = _fast_cmd(link, kwp, SUBFN_FASTLOG,
                             bytes([1]) + struct.pack("<H", flag_addr(link)), 10400,
                             window=0.3,
                             send_baud=fast_baud if attempt < 2 else 10400)
            if w is not None:
                ok = True
                break
        except Exception:                           # noqa: BLE001
            pass
        time.sleep(0.1)
    try:
        link.set_baud(10400)
        link.clear_rx()
    except Exception:                               # noqa: BLE001
        pass
    if ok:
        log("EDC15-fast: ECU returned to 10400 baud.")
    else:
        log("EDC15-fast: could not confirm the ECU is back at 10400 baud -- the "
            "next fast connect will try to recover it.")
    return ok


def _nrc_of(cap):
    """The NRC in a ``7F 10 <nrc>`` negative response inside ``cap``, else None."""
    b = bytes(cap or b"")
    i = b.find(b"\x7f\x10")
    return b[i + 2] if 0 <= i < len(b) - 2 else None


def fast_stats(link, reset=True):
    """Round-trip timing of the raw-RAM reads since the last call, as a short
    status fragment: read mode, requests, failures, mean and best ms per
    request. Tells a per-request ECU/transport floor apart from client overhead."""
    st = getattr(link, "_fl_stats", None)
    if not st or not st["n"]:
        return ""
    mode = {True: "burst", False: "split"}.get(getattr(link, "_fl_burst_ok", None), "?")
    if getattr(link, "_fl_stored_ok", None):
        mode = "list+" + mode
    out = "%s/%s, %d req, %d failed, %.0f ms/req (best %.0f)" % (
        mode, getattr(link, "_fl_timing", "?").split(" [")[0], st["n"], st["bad"], 1000.0 * st["t"] / st["n"],
        1000.0 * st["min"] if st["min"] < 9 else 0.0)
    d = getattr(link, "_fl_diag", None)
    if d and d["rows"]:
        out += ", slow rows %d/%d" % (d["slow"], d["rows"])
    if reset:
        link._fl_stats = None
    return out


def fast_read_split(link, kwp, addrs, fast_baud, window=0.2):
    """Read each address with its OWN n=1 request and merge into {addr: word}.

    The multi-address (n>1) burst gets no reply on-car even when paced, while a
    single-word read is the exact path the on-car validate proved byte-identical.
    So the live logger reads one address per request until the n>1 burst is
    understood. An address that fails to read is simply absent from the result."""
    out = {}
    for a in addrs:
        w = fast_read(link, kwp, [a], fast_baud, first=False, window=window)
        if w and a in w:
            out[a] = w[a]
    return out


MAX_BURST = 16       # addresses per 0x90 request (body = 3 + 2n bytes)


def fast_read_group(link, kwp, addrs, fast_baud, window=0.15, use_list=True):
    """:func:`_read_group`, timed as one row for the session diagnostics."""
    _diag(link)["events"] = []
    t0 = time.perf_counter()
    try:
        return _read_group(link, kwp, addrs, fast_baud, window, use_list)
    finally:
        _diag_row(link, time.perf_counter() - t0)


def _read_group(link, kwp, addrs, fast_baud, window=0.15, use_list=True):
    """Read ``addrs`` for one measuring group as fast as the ECU allows: try the
    single n-word burst first (one round-trip for the whole group), and if the
    ECU does not answer it, fall back to one request per address -- and remember
    that, so a setup that never answers the burst is not re-probed every row."""
    if not addrs:
        return {}
    out = {}
    # 13..16 addresses fit ONE 0x90 burst, which beats list(12) + a second request.
    if use_list and not (STORED_MAX < len(addrs) <= MAX_BURST):
        # v2.6 flash: the first STORED_MAX addresses go through the ECU-side list
        # (a 3-byte request per row once stored); any rest, or a flash without
        # the feature, uses the plain 0x90 bursts below.
        got = _try_stored(link, kwp, addrs, fast_baud, window)
        if got is not None:
            out.update(got)
            addrs = addrs[STORED_MAX:]
            if not addrs:
                return out
    step = getattr(link, "_fl_max", MAX_BURST)
    for i in range(0, len(addrs), step):
        part = addrs[i:i + step]
        if len(part) > 1 and getattr(link, "_fl_burst_ok", None) is not False:
            got = None
            for _try in range(3):
                got = fast_read(link, kwp, part, fast_baud, window=window)
                if got is not None and len(got) >= len(part):
                    break
                got = None
                # Only NRC 0x12 means "this flash rejects n>1" (pre-v2.5). Anything
                # else -- no reply, or NRC 0x13 after the ECU timed out on a send
                # that stalled between bytes -- is a transient: let the ECU's
                # receiver resync and send the SAME burst again. (A transient used
                # to flip the whole session to one-request-per-address for good.)
                if getattr(link, "_fl_nrc", None) == 0x12:
                    link._fl_burst_ok = False
                    break
                time.sleep(0.06)
            if got is not None:
                link._fl_burst_ok = True
                out.update(got)
                continue
            if len(part) > 4 and getattr(link, "_fl_burst_ok", None) is not False:
                # the ECU's receive buffer may not hold this many: halve (16->8->4)
                link._fl_max = max(4, len(part) // 2)
        out.update(fast_read_split(link, kwp, part, fast_baud, window=window))
    return out


def _burst_read_group(link, kw, group, window=0.12):
    """Request a measuring group over KW1281, then read the ECU's response as a
    RAW continuous stream WITHOUT sending a per-byte complement. If the firmware
    advances its block send on the TX echo (not on the tester's complement), the
    whole [len][ctr][E7][data] block arrives back-to-back and this returns it.
    If the firmware stalls waiting for a complement, only the first byte or two
    come back. No flash change -- purely a client-side probe."""
    kw.send_block([0x29, group & 0xFF])   # request (tester->ECU keeps its complements)
    return _drain(link, window, maxn=64)


def run_bringup(link, log=None, is_stop=None, fast_baud=FAST_BAUD, window=0.6):
    log = log or (lambda m: None)
    r = BringupResult()
    r.baud = fast_baud

    def L(msg):
        log(msg)
        r.note(msg)

    def stopped():
        return bool(is_stop and is_stop())

    # wake: KW1281 9600 -> EndCommunication, then KWP2000 10400
    link.set_baud(9600)
    link.clear_rx()
    ver = link.wake_up(0x01, even_parity=False)
    L("KW1281 wake: KW %d" % ver)
    kw = _kwp.Kw1281(link, log)
    kw.connect()
    kw.end_communication()
    time.sleep(1.0)
    if stopped():
        return r

    link.set_baud(10400)
    link.clear_rx()
    ver = link.wake_up(0x01, even_parity=False)
    L("KWP2000 wake: KW %d" % ver)
    if ver < 2000:
        raise KLineError("could not reach a KWP2000 session (KW %d)" % ver)
    kwp = _kwp.Kwp2000(link, 0x01, log)
    kwp.send_receive(SVC_START_DIAG, b"\x89")
    L("startDiagnosticSession 0x89: ok (extended, engine-running session)")

    # Fire 0x90 at 10400: the hook arms the fast flag (0xD086=0xA55A) and builds the
    # frame; the patched responder (0x0D6A, the [CED2] TX start) sends it at the fast
    # baud. Switch up IMMEDIATELY (no sleep) so we are at the fast baud before the
    # response arrives (P2 ~25 ms); kwp.send already drained the 10400 echo. Then fall
    # back to a 10400 read to catch the response if the baud did NOT change.
    s0bg = _s0bg_for(fast_baud)
    addrs = RAM_ADDRS
    body = bytes([SUBFN_FASTLOG, s0bg, len(addrs)])
    for a in addrs:
        body += bytes([a & 0xFF, a >> 8])         # little-endian RAM address
    link.clear_rx()
    kwp.send(SVC_START_DIAG, body)
    L("sent 10 90 %02X n=%d %s at 10400 (baud %d -> S0BG 0x%02X; raw-RAM read list)"
      % (s0bg, len(addrs), " ".join("%04X" % a for a in addrs), fast_baud, s0bg))
    link.set_baud(fast_baud)
    r.cap1 = _drain(link, window, maxn=160)
    L("%d capture (%d): %s" % (fast_baud, len(r.cap1), r.cap1.hex(" ") or "(none)"))
    r.words = _decode_frame(r.cap1)
    where = fast_baud
    if r.words is None:
        # fall back: did it answer at 10400 (baud change did not take)?
        link.set_baud(10400)
        r.cap2 = _drain(link, window, maxn=160)
        L("10400 fallback (%d): %s" % (len(r.cap2), r.cap2.hex(" ") or "(none)"))
        w2 = _decode_frame(r.cap2)
        if w2 is not None:
            r.words = w2; where = 10400
    if r.words is not None:
        r.hook_ok = True
        r.marker_ok = bool(r.words) and r.words[0] == MARKER
        L("HOOK FRAME at %d baud: marker=0x%04X%s"
          % (where, r.words[0], " (OK)" if r.marker_ok else " (CORRUPT)"))
    r.ok = r.marker_ok and where == fast_baud
    L("RESULT hook=%s marker=%s where=%s target=%d -> %s"
      % (r.hook_ok, r.marker_ok, where, fast_baud,
         "HIGH-BAUD OK" if r.ok else ("FRAME-AT-10400" if r.hook_ok else "SEE LOG")))
    return r


# --------------------------------------------------------------------------
# v1.3 GROUP fast-log mode
#
# The 0x91 hook calls the firmware's own measuring-group reader (0xA9488) with
# the requested group number, so the block it builds is byte-for-byte what a
# normal slow KW1281 "29 <group>" read returns -- [len][ctr][0xE7][data] -- but
# it is streamed back over the fast link instead of the ACK-per-byte KW1281
# protocol. That means the existing parse_cells() decodes it unchanged; only the
# transport and baud differ. The hook writes the reader's block at buf+5 (past
# the [fmt][tgt][src][0x50][0x90] response header), so the client finds it right
# after the usual 50 90 marker.
# --------------------------------------------------------------------------

def _fmt_cells(cells):
    if not cells:
        return "(none)"
    return " | ".join("fid=0x%02X a=%d b=%d (%s)" % (c.fid, c.a, c.b, c.text)
                      for c in cells)


def _find_group_block(buf):
    """Locate the 50 90 marker in a fast-link capture and return the KW1281
    measuring block that follows it as (block_bytes, cells). The block is
    [len][ctr][0xE7][data...]; bounded by its leading length byte."""
    from .diag import parse_cells
    i = buf.find(MARK)
    if i < 0:
        return b"", []
    blk = bytes(buf[i + 2:])
    if len(blk) < 3:
        return blk, []
    ln = blk[0]
    if 3 <= ln <= 64 and len(blk) >= ln:
        blk = blk[:ln]
    if len(blk) < 3 or blk[2] != GROUP_TITLE:
        return blk, []
    return blk, parse_cells(blk[3:], drop_echo=False)


def run_group_bringup(link, log=None, is_stop=None, group=0x01,
                      fast_baud=FAST_BAUD, window=0.6):
    """Bench test for v1.3 GROUP fast-log mode. Reads one measuring block the
    slow KW1281 way first (reference), then the fast way via the 0x91 hook, and
    compares the raw E7 blocks. Requires the v1.3 flash (checkpoint e1ec8c4f20).

    Returns a dict with slow_block/fast_block/slow_cells/fast_cells/match/ok."""
    from .diag import parse_cells
    log = log or (lambda m: None)
    steps = []

    def L(m):
        log(m)
        steps.append(m)

    def stopped():
        return bool(is_stop and is_stop())

    r = {"group": group, "baud": fast_baud, "steps": steps,
         "slow_block": b"", "fast_cap": b"", "fast_block": b"",
         "slow_cells": [], "fast_cells": [], "where": None,
         "match": False, "ok": False}

    # --- KW1281 9600: reference slow group read ---
    link.set_baud(9600)
    link.clear_rx()
    ver = link.wake_up(0x01, even_parity=False)
    L("KW1281 wake: KW %d" % ver)
    kw = _kwp.Kw1281(link, log)
    kw.connect()
    try:
        kw.send_block([0x29, group & 0xFF])
        sblk = bytes(kw.receive_block())
        r["slow_block"] = sblk
        L("slow KW1281 group %d: %s" % (group, sblk.hex(" ")))
        if len(sblk) >= 3 and sblk[2] == GROUP_TITLE:
            r["slow_cells"] = parse_cells(sblk[3:], drop_echo=False)
            L("  slow cells: %s" % _fmt_cells(r["slow_cells"]))
        else:
            L("  slow read: group %d is not an E7 block (unavailable here)" % group)
        # No-flash probe: does the ECU stream the block without per-byte complements?
        try:
            burst = _burst_read_group(link, kw, group)
            L("BURST (no-complement) read of group %d: %d bytes: %s"
              % (group, len(burst), burst.hex(" ") or "(none)"))
            bi = burst.find(bytes([GROUP_TITLE]))
            sd = r["slow_block"][2:] if len(r["slow_block"]) >= 3 else b""
            if bi >= 0 and sd and burst[bi:bi + len(sd)] == sd:
                L("  -> BURST OK: full E7 block streamed WITHOUT complements "
                  "(firmware is echo-paced; fast logging is client-side at 9600, "
                  "faster still at raised baud)")
            elif len(burst) <= 3:
                L("  -> BURST STALLED after %d bytes: ECU waits for the tester "
                  "complement; a de-ACK flash patch is required" % len(burst))
            else:
                L("  -> BURST PARTIAL/mismatch (%d bytes) -- inspect raw" % len(burst))
        except (KLineError, KLineTimeout, OSError) as e:
            L("burst probe error: %r" % e)
    except (KLineError, KLineTimeout, OSError) as e:
        L("slow read error: %r" % e)
    kw.end_communication()
    # The burst (no-complement) probe above is the decisive test right now; stop here so
    # the log is always saved and we do NOT fire the 0x91 hook (which resets the ECU).
    # Re-enable the KWP2000 section below once the transport question is settled.
    return r

    # --- KWP2000 10400 -> extended -> 0x91 fast group read ---
    link.set_baud(10400)
    link.clear_rx()
    ver = link.wake_up(0x01, even_parity=False)
    L("KWP2000 wake: KW %d" % ver)
    if ver < 2000:
        raise KLineError("could not reach a KWP2000 session (KW %d)" % ver)
    kwp = _kwp.Kwp2000(link, 0x01, log)
    kwp.send_receive(SVC_START_DIAG, b"\x89")
    L("startDiagnosticSession 0x89: ok (extended, engine-running session)")

    # --- localize the 0x91 reset (run BEFORE 0x91, which may reset the ECU) ---
    spw, _ = _fast_cmd(link, kwp, 0x92, b"", fast_baud)
    if spw and len(spw) >= 2:
        L("DIAG 0x92 SP@entry=0x%04X  guard=0x%04X  headroom=%d bytes"
          % (spw[0], spw[1], spw[0] - spw[1]))
        r["sp_entry"] = spw[0]
    else:
        L("DIAG 0x92 SP ping: no response (cap decode failed)")
    a6, _ = _fast_cmd(link, kwp, 0x93, bytes([group & 0xFF]), fast_baud)
    if a6 and a6[0] == 0xCAFE:
        L("DIAG 0x93 A680 probe: OK (returned 0xCAFE) -> A680 is safe; the reset is in A746")
        r["a680_ok"] = True
    elif a6:
        L("DIAG 0x93 A680 probe: unexpected 0x%04X" % a6[0])
    else:
        L("DIAG 0x93 A680 probe: NO response -> A680/FF16 resets the ECU (reset is in A680)")
        r["a680_ok"] = False

    s0bg = _s0bg_for(fast_baud)
    link.clear_rx()
    kwp.send(SVC_START_DIAG, bytes([SUBFN_GROUP, s0bg, group & 0xFF]))
    L("sent 10 91 %02X %02X at 10400 (fast group read; baud %d -> S0BG 0x%02X)"
      % (s0bg, group & 0xFF, fast_baud, s0bg))
    link.set_baud(fast_baud)
    cap = _drain(link, window, maxn=192)
    r["fast_cap"] = cap
    L("%d capture (%d): %s" % (fast_baud, len(cap), cap.hex(" ") or "(none)"))
    fblk, fcells = _find_group_block(cap)
    where = fast_baud
    if not fblk or fblk[2:3] != bytes([GROUP_TITLE]):
        link.set_baud(10400)
        cap2 = _drain(link, window, maxn=192)
        L("10400 fallback (%d): %s" % (len(cap2), cap2.hex(" ") or "(none)"))
        fblk2, fcells2 = _find_group_block(cap2)
        if fblk2 and fblk2[2:3] == bytes([GROUP_TITLE]):
            fblk, fcells, where = fblk2, fcells2, 10400
    r["fast_block"] = fblk
    r["fast_cells"] = fcells
    r["where"] = where if fblk else None
    if fblk and fblk[2:3] == bytes([GROUP_TITLE]):
        L("fast group %d block @ %d baud: %s" % (group, where, fblk.hex(" ")))
        L("  fast cells: %s" % _fmt_cells(fcells))
        sd = r["slow_block"][2:] if len(r["slow_block"]) >= 3 else b""
        fd = fblk[2:]
        r["match"] = bool(fd) and bool(sd) and sd[:len(fd)] == fd
        r["ok"] = r["match"] and where == fast_baud
        L("RESULT: fast E7 block %s the slow block; baud=%d target=%d -> %s"
          % ("MATCHES" if r["match"] else "DIFFERS from", where, fast_baud,
             "GROUP FAST-LOG OK" if r["ok"] else
             ("MATCH BUT AT 10400" if r["match"] else "SEE RAW")))
    else:
        L("RESULT: no fast E7 group block captured (cap=%s)" % (cap.hex(" ") or "(none)"))

    # --- v1.5 diagnostic: breadcrumb + saved flags via the proven raw-RAM 0x90 path ---
    # 0xD0AC == 0xBEEF  iff the 0x91 hook ran to completion (so the response WAS built and
    # the failure is the KWP2000 responder not transmitting -- a state/routing issue);
    # anything else (or no frame at all) means the hook aborted / the ECU reset.
    # 0xD0B0 = CF76 and 0xD0B8(low) = CFBF as captured at hook entry (the idle values).
    try:
        link.set_baud(10400)
        link.clear_rx()
        diag_addrs = [0xD0AC, 0xD0B0, 0xD0B8]
        body = bytes([SUBFN_FASTLOG, s0bg, len(diag_addrs)])
        for a in diag_addrs:
            body += bytes([a & 0xFF, a >> 8])
        kwp.send(SVC_START_DIAG, body)
        link.set_baud(fast_baud)
        dcap = _drain(link, window, maxn=96)
        dw = _decode_frame(dcap)
        if dw is None:
            link.set_baud(10400)
            dw = _decode_frame(_drain(link, window, maxn=96))
        r["diag"] = dw
        if dw and len(dw) >= 3:
            L("DIAG 0xD0AC=0x%04X (0xBEEF=hook completed)  saved CF76=0x%04X CFBF=0x%02X"
              % (dw[0], dw[1], dw[2] & 0xFF))
            L("  -> %s" % ("HOOK COMPLETED: block built but responder did not TX (routing/state)"
                           if dw[0] == 0xBEEF else
                           "HOOK DID NOT COMPLETE (reset/abort before breadcrumb)"))
        else:
            L("DIAG raw-RAM probe: no frame (session may have reset; cap=%s)"
              % (dcap.hex(" ") or "(none)"))
    except Exception as e:          # noqa: BLE001
        L("DIAG probe error: %r" % e)
    return r


def run_fastlog_validate(link, cells, log=None, is_stop=None, group=0x01,
                         fast_baud=FAST_BAUD, window=0.5):
    """On-car, READ-ONLY validation of the client-side fast-log path against
    the firmware's own slow read, for one measuring block.

    ``cells`` is the block's :class:`ovtsuite.edc15.fastlog15.FastCell` list,
    resolved offline from the open binary. The check:

      1. KW1281 slow-read ``group`` -> the firmware's cells (the reference).
      2. KWP2000 extended + the 0x90 raw-RAM burst reads the cells' RAM
         addresses at ``fast_baud``.
      3. Rebuild the cells client-side (``block_body`` + scaler) and compare
         the ``(fid, a, b)`` bytes to the slow read.

    Nothing is written to the ECU; it needs the v1.2 raw-RAM flash
    (checkpoint 2d27d9a13d). Returns a dict with the two cell lists, the raw
    RAM words, a per-cell comparison and an overall ``ok``.
    """
    from .diag import parse_cells
    from ..edc15 import fastlog15 as _fl
    log = log or (lambda m: None)
    steps = []

    def L(m):
        log(m); steps.append(m)

    r = {"group": group, "baud": fast_baud, "steps": steps, "slow_block": b"",
         "slow_cells": [], "fast_cells": [], "words": {}, "compare": [],
         "ok": False}
    addrs = _fl.read_addrs(cells)
    L("fast-log validate: group %d, %d populated cell(s), offline read_ram %s"
      % (group, len(addrs), ", ".join("0x%04X" % a for a in addrs) or "(none)"))
    wmap = {}

    # --- 1. KW1281: preamble handshake + slow-read the group. Gives the
    #        reference cells AND makes the firmware resolve the group, leaving
    #        the real (readptr, paramA, paramB) per cell in the CF1E block.
    #        Done BEFORE any 0x90 so the baud flag isn't armed during KW1281. ---
    try:
        link.set_baud(9600); link.clear_rx()
        link.wake_up(0x01, even_parity=False)
        kw = _kwp.Kw1281(link, log); kw.connect()
        try:
            kw.send_block([0x29, group & 0xFF])
            sblk = bytes(kw.receive_block())
            r["slow_block"] = sblk
            if len(sblk) >= 3 and sblk[2] == GROUP_TITLE:
                r["slow_cells"] = parse_cells(sblk[3:], drop_echo=False)
                L("slow KW1281 cells: %s" % _fmt_cells(r["slow_cells"]))
            else:
                L("slow read: group %d not an E7 block (unavailable)" % group)
        finally:
            kw.end_communication()
    except (KLineError, KLineTimeout, OSError) as e:
        L("slow read error: %r" % e)
        return r
    time.sleep(1.0)          # settle after EndCommunication before re-init (run_bringup timing)
    if is_stop and is_stop():
        return r

    # --- 2. KWP2000 0x90: read what the firmware actually resolved. CF1E is
    #        external XRAM (hook-readable); CF1E[slot] = (readptr, paramA,
    #        paramB). Compare readptr to our offline read_ram, then try reading
    #        the firmware's own pointers directly. ---
    try:
        link.set_baud(10400); link.clear_rx()
        ver = link.wake_up(0x01, even_parity=False)
        if ver < 2000:
            L("could not reach KWP2000 (KW %d)" % ver)
            return r
        kwp = _kwp.Kwp2000(link, 0x01, log)
        kwp.send_receive(SVC_START_DIAG, b"\x89")
    except (KLineError, KLineTimeout, OSError) as e:
        L("KWP wake/session failed: %r (slow cells above still valid)" % e)
        return r

    _sent = {"n": 0}

    def _rd(addr_list, win=window):
        # First 0x90 is sent at 10400 (ECU still there); it leaves the ECU at the
        # fast baud, so every read after it is sent at fast_baud.
        sb = 10400 if _sent["n"] == 0 else fast_baud
        _sent["n"] += 1
        p = bytes([len(addr_list)]) + b"".join(struct.pack("<H", a) for a in addr_list)
        w, _ = _fast_cmd(link, kwp, SUBFN_FASTLOG, p, fast_baud, win, send_baud=sb)
        return w

    # First read @10400 establishes the fast baud (liveness); then read each
    # cell's read_ram at the fast baud and apply the client-side scaler.
    _lv = _rd([0xD086])
    L("hook-liveness (read 0xD086): %s"
      % ("0x%04X %s" % (_lv[0], "OK" if _lv[0] == 0xA55A else "(exp A55A)") if _lv
         else "NO RESPONSE"))
    # Cross-check the firmware's own resolved pointer for cell 0 (CF1E+0).
    _cfp = _rd([0xCF1E])
    if _cfp and addrs:
        L("  CF1E+0 firmware readptr = 0x%04X  (offline read_ram cell0 = 0x%04X) %s"
          % (_cfp[0], addrs[0], "MATCH" if _cfp[0] == addrs[0] else "DIFF"))
    L("fast raw-RAM read of cell read_ram values:")
    for i, c in enumerate(cells):
        if c.empty:
            continue
        _w = _rd([c.read_addr])
        if _w:
            wmap[c.read_addr] = _w[0]
            L("  cell%d read_ram 0x%04X = 0x%04X" % (i, c.read_addr, _w[0]))
        else:
            L("  cell%d read_ram 0x%04X = NO RESPONSE" % (i, c.read_addr))
    if wmap:
        r["words"] = {("0x%04X" % a): v for a, v in wmap.items()}

    # --- 3. rebuild client-side and compare ---
    body = _fl.block_body(cells, wmap)
    r["fast_cells"] = parse_cells(body, drop_echo=False)
    L("fast  (client) cells: %s" % _fmt_cells(r["fast_cells"]))
    cmp = []
    allok = bool(r["slow_cells"])
    for i, c in enumerate(cells):
        s = r["slow_cells"][i] if i < len(r["slow_cells"]) else None
        fb = body[3 * i:3 * i + 3]
        if s is None:
            cmp.append((i, "no slow cell", False)); allok = False; continue
        sb = bytes([s.fid, s.a, s.b])
        same = (fb == sb)
        allok = allok and same
        cmp.append((i, "slow %s vs fast %s %s"
                    % (sb.hex(" "), fb.hex(" "), "OK" if same else "DIFF"), same))
        L("  cell %d: %s" % (i, cmp[-1][1]))
    r["compare"] = cmp
    r["ok"] = allok
    L("VERDICT: %s" % ("MATCH -- client fast-log reproduces the slow read"
                       if allok else "mismatch (see per-cell; rounding/RAM semantics)"))
    return r


def run_fast_group_test(link, log=None, is_stop=None, group=0x01,
                        fast_baud=FAST_BAUD, window=0.25):
    """Option B (v1.8 flash, checkpoint 035382b885): KW1281 de-ACK burst group read.
    Reads the group the slow KW1281 way (reference), arms the burst with '29 0xFB',
    switches to the high baud, then reads the group as a de-ACK'd burst and compares.
    Returns a dict (slow_block/fast_block/cells/match/ok/dt_ms)."""
    import time as _t
    from .diag import parse_cells
    log = log or (lambda m: None)
    steps = []
    def L(m):
        log(m); steps.append(m)
    r = {"group": group, "baud": fast_baud, "steps": steps, "slow_block": b"",
         "fast_block": b"", "cells": [], "match": False, "ok": False, "dt_ms": None}

    link.set_baud(9600); link.clear_rx()
    ver = link.wake_up(0x01, even_parity=False)
    L("KW1281 wake: KW %d" % ver)
    kw = _kwp.Kw1281(link, log); kw.connect()

    # reference slow read
    try:
        kw.send_block([0x29, group & 0xFF])
        sblk = bytes(kw.receive_block())
        r["slow_block"] = sblk
        L("slow group %d: %s" % (group, sblk.hex(" ")))
        if len(sblk) >= 3 and sblk[2] == GROUP_TITLE:
            L("  slow cells: %s" % _fmt_cells(parse_cells(sblk[3:], drop_echo=False)))
    except (KLineError, KLineTimeout, OSError) as e:
        L("slow read error: %r" % e)

    # arm the de-ACK burst: 29 FB at 9600; the ECU arms and bursts its (empty) reply
    # at the high baud, so switch up immediately and drain it.
    try:
        kw.send_block([0x29, MAGIC_ARM_GROUP])
    except (KLineError, KLineTimeout, OSError):
        pass
    link.set_baud(fast_baud)
    armcap = _drain(link, window, maxn=64)
    L("armed 29 %02X; switched to %d baud; arm drain %d bytes: %s"
      % (MAGIC_ARM_GROUP, fast_baud, len(armcap), armcap.hex(" ") or "(none)"))
    if len(armcap) >= 2:
        kw._counter = (armcap[1] + 1) & 0xFF   # resync block counter from the burst
        L("  synced block counter to 0x%02X" % kw._counter)

    # fast read: request at the high baud, then read the de-ACK burst response raw
    try:
        t0 = _t.monotonic()
        kw.send_block([0x29, group & 0xFF])          # request (ECU complements our bytes)
        cap = _drain(link, window, maxn=64)          # response arrives as a burst, no complements
        r["dt_ms"] = (_t.monotonic() - t0) * 1000.0
    except (KLineError, KLineTimeout, OSError) as e:
        L("fast read error: %r" % e)
        cap = b""
    L("fast capture %d bytes in %.0f ms: %s"
      % (len(cap), r["dt_ms"] or 0, cap.hex(" ") or "(none)"))
    bi = cap.find(bytes([GROUP_TITLE]))
    if bi >= 1:
        blk = cap[bi - 2:]                           # [len][ctr][E7][data...]
        ln = blk[0] if blk else 0
        if 3 <= ln <= 64 and len(blk) >= ln:
            blk = blk[:ln]
        r["fast_block"] = bytes(blk)
        if len(blk) >= 2:
            kw._counter = (blk[1] + 1) & 0xFF
        r["cells"] = parse_cells(blk[3:], drop_echo=False)
        L("fast block: %s" % blk.hex(" "))
        L("  fast cells: %s" % _fmt_cells(r["cells"]))
        sd = r["slow_block"][2:] if len(r["slow_block"]) >= 3 else b""
        fd = blk[2:]
        r["match"] = bool(fd) and bool(sd) and sd[:len(fd)] == fd
        r["ok"] = r["match"]
        L("RESULT: fast burst %s slow block; %.0f ms -> %s"
          % ("MATCHES" if r["match"] else "DIFFERS from", r["dt_ms"] or 0,
             "GROUP BURST OK" if r["ok"] else "SEE RAW"))
    else:
        L("RESULT: no E7 block in burst (cap=%s)" % (cap.hex(" ") or "(none)"))
    try:
        link.set_baud(9600); kw.end_communication()
    except Exception:
        pass
    return r
