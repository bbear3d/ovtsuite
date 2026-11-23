# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 research probes: non-destructive tools that answered questions about
the ECU while the flasher was being built.

Which security level reads boot (none), which ranges the 0x31 C5 checksum
accepts, which SecurityAccess levels exist, RAM / coding / immobiliser reads
over readMemoryByAddress (0x23), finding the EEPROM, and the Scanmatik SM2
crank/cam (GPT) test. Their findings are in
``tools/j2534logger/EDC16_ODIS_flash_protocol.md``.

The flasher does not offer them (``gui_edc16.RESEARCH_TOOLS``); the code is kept
to come back to. Nothing here erases or writes flash.
"""
from . import edc16_blocks as B
from .edc16 import encrypt, StopRequested, KLineError

# K-line read session (LVL3) and programming session (LVL1) come from
# Edc16Flash.open_read_session / open_program_session.


# Addresses to probe. Boot/low is the region the read session (LVL3) is refused
# on with NRC 0x33 (securityAccessDenied) -- the disassembly (gate 0x7c584) proves
# that is a per-region security-LEVEL gate, so a different level may grant it.
# 0x40000 (program) is the readable control: it must stay granted at every level.
_BOOT_PROBE_ADDR = 0x010000
_PROGRAM_CTRL_ADDR = 0x040000


def boot_read_probe(fl, *, addr=_BOOT_PROBE_ADDR, speed="Medium", allow_fast=True,
                    on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: find which security level and/or read SERVICE can READ the
    boot/low region over OBD.

    Two independent read paths are tested per region, because they go through
    DIFFERENT per-region security gates:
      * requestUpload (0x35)        -- gate 0x7c584, region_table @ r14-0x654
      * readMemoryByAddress (0x23)  -- handler 0xB1360, region_table @ r14-0x700
    Boot is refused via 0x35 at every level we hold, but the 0x23 gate is separate,
    so 0x23 may read boot even when 0x35 won't. Each is tried at LVL3 (read), LVL1
    (write/SA2), LVL5 (seed+K5, K5 read live) and LVL9 (LFSR). program (0x40000) is
    the readable control. Nothing is written; 0x23 reads only 0x20 bytes.

    Returns {level: {"boot": bool, "boot_rma": bool, "program": bool,
    "program_rma": bool}}."""
    on_stage = on_stage or (lambda _m: None)
    results = {}

    def _try(name):
        row = {}
        for rname, raddr in (("boot", addr), ("program", _PROGRAM_CTRL_ADDR)):
            if is_stop and is_stop():
                raise StopRequested("boot-probe stopped")
            # requestUpload (0x35)
            try:
                fl._request_upload(raddr, 0x100)
                row[rname] = True
                on_stage("  [%s] %s 0x%06X upload(0x35) -> GRANTED" % (name, rname, raddr))
                try:
                    fl._request_transfer_exit()
                except Exception:
                    pass
            except Exception as ex:
                row[rname] = False
                on_stage("  [%s] %s 0x%06X upload(0x35) -> refused (%s)" % (name, rname, raddr, ex))
            if is_stop and is_stop():
                raise StopRequested("boot-probe stopped")
            # readMemoryByAddress (0x23) -- the separate gate
            try:
                data = fl._read_memory_by_address(raddr, 0x20)
                row[rname + "_rma"] = True
                on_stage("  [%s] %s 0x%06X readMem(0x23) -> READ %s..."
                         % (name, rname, raddr, bytes(data[:8]).hex()))
            except Exception as ex:
                row[rname + "_rma"] = False
                on_stage("  [%s] %s 0x%06X readMem(0x23) -> refused (%s)" % (name, rname, raddr, ex))
        results[name] = row

    on_stage("Opening read session (%s)..." % fl.bus_name())
    fl.open_read_session(speed, allow_fast, is_stop, reopen=False)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")
    _try("LVL3(read 27 03/04)")

    for name, auth in (("LVL1(write 27 01/02)", fl._lvl1_auth),
                       ("LVL5(27 05/06)", fl._lvl5_auth),
                       ("LVL9(27 09/0A)", fl._lvl9_auth)):
        if is_stop and is_stop():
            break
        on_stage("Re-authenticating at %s..." % name)
        try:
            auth()
            on_stage("  %s auth OK." % name)
            _try(name)
        except Exception as ex:
            on_stage("  %s auth refused (%s)." % (name, ex))
            results[name] = {"boot": False, "boot_rma": False,
                             "program": None, "program_rma": None, "error": str(ex)}

    try:
        fl._try_quiet_stop()
    except Exception:
        pass

    hit = []
    for lv, r in results.items():
        if r.get("boot"):
            hit.append("%s via upload(0x35)" % lv)
        if r.get("boot_rma"):
            hit.append("%s via readMem(0x23)" % lv)
    if hit:
        on_stage("RESULT: boot IS readable -- %s." % "; ".join(hit))
    else:
        on_stage("RESULT: boot refused by BOTH services at every level tested.")
    return results


# Level-5 additive constant K5 lives in the RAM coding struct (0x7FE9E4), at
# +0x27 (a u16).  It is a per-ECU coding/EEPROM value, not a flash constant, so
# it must be read from the live ECU.  See the protocol doc's level-5 section.
CODING_STRUCT_ADDR = 0x7FE9E4
K5_STRUCT_OFFSET = 0x27


def ram_read_probe(fl, *, addr=CODING_STRUCT_ADDR, size=0x40, speed="Medium",
                   allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: read `size` bytes at 24-bit RAM `addr` via
    readMemoryByAddress (0x23), trying each security level we hold a key for
    (LVL3 read, then LVL1 write, then LVL9).  Reads only -- nothing is written.

    Primary use: pull the level-5 additive constant K5 = the u16 at the coding
    struct +0x27 (default addr 0x7FE9E4).  Secondary use: probe whether RAM (and
    thus the EEPROM shadow) is readable over OBD at all, and at which level.

    Returns {level_label: {"data": bytes|None, "k5": int|None, "error": str|None}}.
    """
    on_stage = on_stage or (lambda _m: None)
    results = {}
    is_coding = (addr == CODING_STRUCT_ADDR and size > K5_STRUCT_OFFSET + 1)

    def _try(name):
        row = {"data": None, "k5": None, "error": None}
        try:
            data = fl._read_memory_by_address(addr, size)
            row["data"] = data
            on_stage("  [%s] 0x%06X +0x%X -> %s" % (name, addr, size, data.hex()))
            if is_coding and len(data) > K5_STRUCT_OFFSET + 1:
                k5 = (data[K5_STRUCT_OFFSET] << 8) | data[K5_STRUCT_OFFSET + 1]
                row["k5"] = k5
                on_stage("    -> K5 = 0x%04X  => level-5 key = (seed + 0x%04X) & 0xFFFFFFFF"
                         % (k5, k5))
        except Exception as ex:
            row["error"] = str(ex)
            on_stage("  [%s] 0x%06X refused (%s)" % (name, addr, ex))
        results[name] = row
        return row["data"] is not None

    on_stage("Opening read session (%s)..." % fl.bus_name())
    fl.open_read_session(speed, allow_fast, is_stop, reopen=False)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")
    if not _try("LVL3(read 27 03/04)"):
        for name, auth in (("LVL1(write 27 01/02)", fl._lvl1_auth),
                           ("LVL9(27 09/0A)", fl._lvl9_auth)):
            if is_stop and is_stop():
                break
            try:
                auth()
                on_stage("  %s auth OK." % name)
                if _try(name):
                    break
            except Exception as ex:
                on_stage("  %s auth refused (%s)." % (name, ex))
                results[name] = {"data": None, "k5": None, "error": "auth: %s" % ex}
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    ok = [lv for lv, r in results.items() if r.get("data") is not None]
    if ok:
        on_stage("RESULT: RAM 0x%06X readable at %s." % (addr, ", ".join(ok)))
    else:
        on_stage("RESULT: RAM 0x%06X not readable at any level we hold "
                 "(readMemoryByAddress refused)." % addr)
    return results


# ---------------------------------------------------------------------------
# Immo / coding read (LVL3) -- dump the coding image + ImmCtl state and decode
# the immobiliser status byte.  NON-DESTRUCTIVE.  The region addresses are
# candidates derived from static disassembly of 03G906016LQ; a generous span is
# read so the immo byte is captured even if an offset is slightly off, and the
# live values are what confirm/tune them.
IMMO_STATUS_STRUCT_OFFSET = 0x56          # coding struct (0x7FE9E4) +0x56 -> immo status byte
IMMO_STATUS_STATES = {
    0x33: "immobiliser ACTIVE (33)", 0x73: "immobiliser ACTIVE (73)",
    0xF3: "immobiliser ACTIVE (F3)", 0x53: "record invalid (53)",
    0x00: "released (00)", 0x0F: "released (0F)",
    0x60: "OFF / not installed (60)",
}
CODING_MIRROR_ADDR = 0x8012C0             # external-RAM coding image ($3B record tables point here)
IMMO_STATE_ADDR = 0x7F9480                # ImmCtl RAM working state

# Widened after the first live read (2026-09-26): the immo struct at 0x7FE9E4 is
# larger than 0x100 and the code reads immo state/config cells across it (e.g.
# 0x7FEA24, and 0x7FEA3A which it compares to 0xF3 = immo active). coding_mirror
# 0x8012C0 read back empty on the bench ECU (kept for confirmation). A flash cell
# (0x1C0000, cal) is read too as an identity-mapping sanity check for $23.
_IMMO_CODING_REGIONS = [
    ("coding_struct", CODING_STRUCT_ADDR, 0x180),
    ("coding_mirror", CODING_MIRROR_ADDR, 0x80),
    ("immo_state",    IMMO_STATE_ADDR,    0x80),
    ("flash_check",   0x1C0000,           0x20),
]


def _read_span(fl, addr, size, is_stop=None, chunk=0x80):
    """readMemoryByAddress (0x23) an arbitrary span in <=chunk-byte reads."""
    out = bytearray()
    off = 0
    while off < size:
        if is_stop and is_stop():
            raise StopRequested("read stopped")
        n = min(chunk, size - off)
        out.extend(fl._read_memory_by_address(addr + off, n))
        off += n
    return bytes(out)


def find_eeprom(fl, *, lo=0x7F8000, hi=0x800000, step=0x80, speed="Medium",
                allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: locate the 4 KB EEPROM (or its RAM shadow) over OBD by its
    OWN checksum structure. Opens the LVL3 read session, $23-sweeps [lo,hi), then
    has the EDC16 module find the base where many 32-byte blocks satisfy
    (sum16 + BE16 chk) == the canonical counter (see
    :func:`ovtsuite.edc16.eeprom16.locate`). Robust to $23 region remapping: the
    base returned is the $23 address that reads EEPROM content. Reads only.

    Needs the EDC16 module (its EEPROM checksum layout); raises RuntimeError
    without it, before anything is sent.

    Returns {"base": int|None, "hits": int, "immo": (b1,b2), "blocks": {0x140:..,0x160:..}}.
    """
    from ..modules import registry as _registry
    cs = _registry.checksum("edc16")
    if cs is None or not hasattr(cs, "eeprom_locate"):
        raise RuntimeError("Finding the EEPROM uses its checksum layout. %s"
                           % _registry.missing_text("edc16", "the EEPROM scan"))
    on_stage = on_stage or (lambda _m: None)
    fl.open_read_session(speed, allow_fast, is_stop, reopen=False)
    on_stage("Read session open (LVL3). Sweeping 0x%06X-0x%06X for the EEPROM..." % (lo, hi))
    img = {}
    a = lo
    while a < hi:
        if is_stop and is_stop():
            raise StopRequested("scan stopped")
        n = min(step, hi - a)
        try:
            d = fl._read_memory_by_address(a, n)
            for i, v in enumerate(d):
                img[a + i] = v
        except Exception as ex:
            on_stage("  read @%06X failed (%s)" % (a, ex))
        a += step
        if (a - lo) % 0x1000 == 0:
            on_stage("  ...swept to 0x%06X" % a)

    found = cs.eeprom_locate(img, lo, hi)
    out = {"base": found["base"], "hits": found["hits"], "immo": found["immo"],
           "blocks": found["blocks"]}
    if found["base"] is not None:
        base = found["base"]
        b1, b2 = found["immo"]
        on_stage("EEPROM FOUND: base 0x%06X (%d/%d canonical blocks valid)."
                 % (base, found["hits"], found["checked"]))
        on_stage("  immo 0x156/0x176 = 0x%02X/0x%02X -> %s"
                 % (b1 or 0, b2 or 0, "ON" if found["immo_on"] else "off/other"))
        for addr, blk in sorted(found["blocks"].items()):
            on_stage("  block 0x%03X @0x%06X: %s" % (addr, base + addr, blk.hex()))
    else:
        on_stage("EEPROM NOT found in 0x%06X-0x%06X - try a wider/different range." % (lo, hi))
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    return out


def gpt_verify_running(fl, *, rpm=850, samples=6, interval=0.5,
                       crank_pin=None, cam_pin=None, group=None,
                       stop_after=True, speed="Low", allow_fast=False,
                       use_gap_pattern=False, crank_only=True,
                       on_stage=None, is_stop=None):
    """Drive the SM2 crank/cam (GPT) signals and confirm the ECU recognises
    them by reading the RPM measuring block.

    Opens a KWP2000 diagnostic session over ``fl.k`` (the same J2534 device),
    starts the crank+cam PWM on that channel, then samples the engine-speed
    measuring block.  Returns a dict; ``recognized`` is True when the ECU
    reports a plausible non-zero RPM (the direct sign the crank signal is
    seen).  With ``stop_after=False`` the GPT outputs are left running so a
    subsequent ``find_eeprom`` pass runs with the engine "turning".

    This is the pre-flight check before hunting the EEPROM: no point sweeping
    if the ECU never believes it is rotating.
    """
    import time as _t
    from .diag import DiagSession
    from . import sm2_gpt
    on_stage = on_stage or (lambda s: None)
    crank_pin = sm2_gpt.DEFAULT_CRANK_PIN if crank_pin is None else crank_pin
    cam_pin = sm2_gpt.DEFAULT_CAM_PIN if cam_pin is None else cam_pin
    grp = sm2_gpt.RPM_GROUP if group is None else group

    # Ensure the J2534 channel is open so fl.k._ch is populated.
    # _ch is None at construction; it is normally set by wake_up() inside
    # diag.connect(), but we must start GPT *before* opening the KWP session
    # (the SM_PWM_SET IOCTL blocks ~10 s and would expire the EDC16 session
    # timer).  Calling _connect_channel() here opens the PassThru channel
    # only (no KWP init pulse) so we can obtain _ch for set_pwm.
    # diag.connect() will reuse the already-open channel and proceed straight
    # to the fast-init KWP pulse + startDiagnosticSession.
    kl = fl.k
    if getattr(kl, "_ch", None) is None and hasattr(kl, "_connect_channel"):
        kl._connect_channel()
    ch = getattr(kl, "_ch", None)
    if ch is None or not hasattr(ch, "set_pwm"):
        raise KLineError(
            "The active J2534 device/channel does not expose the SM2 PWM "
            "(set_pwm) vendor ioctl - GPT crank/cam needs a Scanmatik SM2.")

    # Start GPT BEFORE opening the KWP session.
    # The SM2 SM_PWM_SET IOCTL blocks for ~10 s while the firmware initialises
    # the GPT hardware.  If we open the diagnostic session first, the EDC16
    # P3 session-alive timer (~5 s) expires during that wait and the ECU
    # closes the session before the first measuring-block read is even sent.
    # Correct order: arm signals -> open fresh session -> read blocks.
    items = sm2_gpt.crank_cam_items(rpm, crank_pin=crank_pin, cam_pin=cam_pin,
                                    crank_only=crank_only)
    _mode = "crank only" if crank_only else "crank+cam"
    on_stage("Starting GPT %s: target %d rpm (crank pin %d%s)."
             " (SM2 firmware init may take ~10 s)"
             % (_mode, rpm, crank_pin,
                "" if crank_only else ", cam pin %d" % cam_pin))
    ch.set_pwm(items, ioctl_id=sm2_gpt.PWM_IOCTL)

    # Optional gap-pattern thread: alternates set_pwm rapidly to simulate
    # the 60-2 missing-tooth so the EDC16 crank decoder can synchronise.
    _gap_stop = None
    if use_gap_pattern:
        on_stage("Gap-pattern mode: starting 60-2 tooth simulation thread...")
        _, _gap_stop = sm2_gpt.gap_thread_start(
            ch, rpm, crank_pin=crank_pin, cam_pin=cam_pin,
            crank_only=crank_only)
        on_stage("  Gap-thread running (IOCTL latency measured on first cycle).")

    on_stage("GPT running. Opening KWP session to read measuring blocks...")

    diag = DiagSession(fl.k, ecu="EDC16", log=on_stage)
    diag.connect(speed=speed, is_stop=is_stop)

    readings = []
    try:
        for i in range(samples):
            if is_stop and is_stop():
                break
            _t.sleep(interval)
            try:
                # First iteration: dump all raw cells so the user can see
                # exactly what the ECU is returning (helps distinguish
                # "no response" from "response with 0 RPM").
                if i == 0:
                    raw = diag.read_group(grp)
                    if raw is None:
                        on_stage("  Group %d: no response (session lost?)." % grp)
                    elif not raw:
                        on_stage("  Group %d: empty response (ECU may not support this group)." % grp)
                    else:
                        parts = []
                        for _c in raw:
                            try:
                                _, _, _txt = _c.decoded
                                parts.append("f%d=%s" % (_c.fid, _txt))
                            except Exception:
                                parts.append("f%d(a=%d,b=%d)" % (_c.fid, _c.a, _c.b))
                        on_stage("  Group %d cells: %s" % (grp, ", ".join(parts)))
                rv = sm2_gpt.engine_speed(diag, grp)
            except (KLineError, Exception) as ex:  # noqa: BLE001 - report, keep sampling
                rv = None
                on_stage("  group read failed (%s)" % ex)
            readings.append(rv)
            # Distinguish None (no response) from 0.0 (ECU replied, not synced)
            if rv is None:
                rv_str = "n/a (no response)"
            elif rv < 1.0:
                rv_str = "0 rpm (ECU sees signal but crank not synchronised)"
            else:
                rv_str = "%.0f rpm" % rv
            on_stage("  RPM readback %d/%d: %s" % (i + 1, samples, rv_str))
    finally:
        if _gap_stop is not None:
            _gap_stop.set()      # stop the gap-pattern thread
        if stop_after:
            try:
                sm2_gpt.stop(ch, crank_pin, cam_pin, crank_only=crank_only)
                on_stage("GPT outputs disabled.")
            except Exception as ex:  # noqa: BLE001
                on_stage("GPT stop failed: %s" % ex)

    seen = [r for r in readings if r]
    recognized = any(r and r > 50 for r in seen)
    result = {"target_rpm": rpm, "readings": readings,
              "max_rpm": max(seen) if seen else 0.0,
              "recognized": recognized, "gpt_running": (not stop_after),
              "crank_pin": crank_pin, "cam_pin": cam_pin,
              "crank_only": crank_only, "items": items}
    if recognized:
        on_stage("ECU RECOGNISES crank/cam: engine speed read back "
                 "(max %.0f rpm). Safe to run EEPROM-find passes." % result["max_rpm"])
    else:
        on_stage("ECU did NOT report RPM - crank not recognised as uniform PWM. "
                 "Likely needs the gap pattern, different pins, or drive levels.")
    return result



def gpt_verify_and_find(fl, *, rpm=850, samples=6, interval=0.5,
                        crank_pin=None, cam_pin=None, group=None,
                        lo=0x7F8000, hi=0x800000, step=0x80,
                        speed="Low", allow_fast=False,
                        use_gap_pattern=False, force_eeprom=False,
                        crank_only=True,
                        on_stage=None, is_stop=None):
    """One-click: drive SM2 crank/cam GPT, confirm the ECU recognises the
    signals via measuring-block RPM, then sweep for the EEPROM with the
    signals still active.

    Sequence
    --------
    1. ``gpt_verify_running(..., stop_after=False)`` -- leaves GPT on.
    2. If ECU reported RPM > 50 (``recognized``): run ``find_eeprom``.
    3. Always disable GPT outputs on exit, even if step 2 raised.

    Returns
    -------
    dict with keys:
      ``gpt``      -- full result dict from :func:`gpt_verify_running`
      ``eeprom``   -- result dict from :func:`find_eeprom`, or ``None`` if
                     not recognised / skipped / stopped
      ``recognized`` -- convenience copy of ``gpt["recognized"]``
    """
    from . import sm2_gpt as _gpt
    on_stage = on_stage or (lambda s: None)
    _crank_pin = _gpt.DEFAULT_CRANK_PIN if crank_pin is None else crank_pin
    _cam_pin   = _gpt.DEFAULT_CAM_PIN   if cam_pin   is None else cam_pin

    # step 1 -- verify; leave GPT running
    gpt_result = gpt_verify_running(
        fl, rpm=rpm, samples=samples, interval=interval,
        crank_pin=_crank_pin, cam_pin=_cam_pin, group=group,
        stop_after=False,
        speed=speed, allow_fast=allow_fast,
        use_gap_pattern=use_gap_pattern, crank_only=crank_only,
        on_stage=on_stage, is_stop=is_stop)

    eeprom_result = None
    try:
        if not gpt_result["recognized"] and not force_eeprom:
            on_stage("ECU did not recognise crank/cam -- skipping EEPROM sweep."
                     " (enable 'Force EEPROM' to try anyway with GPT signals active)")
        elif is_stop and is_stop():
            on_stage("Stopped before EEPROM sweep.")
        else:
            if force_eeprom and not gpt_result["recognized"]:
                on_stage("Force EEPROM: sweeping despite no RPM readback "
                         "(GPT signals still active)...")
            else:
                on_stage("ECU is rotating. Starting EEPROM sweep with GPT active...")
            eeprom_result = find_eeprom(
                fl, lo=lo, hi=hi, step=step,
                speed="Medium", allow_fast=allow_fast,
                on_stage=on_stage, is_stop=is_stop)
    finally:
        # always kill the GPT outputs
        try:
            ch = getattr(fl.k, "_ch", None) or fl.k
            _gpt.stop(ch, _crank_pin, _cam_pin, crank_only=crank_only)
            on_stage("GPT outputs disabled.")
        except Exception as ex:  # noqa: BLE001
            on_stage("GPT stop failed: %s" % ex)

    return {"gpt": gpt_result, "eeprom": eeprom_result,
            "recognized": gpt_result["recognized"]}

def read_immo_coding(fl, *, regions=None, speed="Medium", allow_fast=True,
                     on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: open the LVL3 read session and readMemoryByAddress (0x23)
    the coding struct, the external coding mirror and the ImmCtl RAM state, then
    decode the immobiliser status byte (coding struct +0x56) and K5.

    Enabling first step for immo-off: read the live coding/immo image so the
    writable record and the off-value can be identified before any write.  Reads
    only -- nothing is written.

    Returns {"regions": {name: {"addr", "data"|None, "error"|None}},
             "immo_status": int|None, "immo_text": str, "k5": int|None}.
    """
    on_stage = on_stage or (lambda _m: None)
    regions = regions or _IMMO_CODING_REGIONS
    out = {"regions": {}, "immo_status": None, "immo_text": "unknown", "k5": None}

    on_stage("Opening read session (%s)..." % fl.bus_name())
    fl.open_read_session(speed, allow_fast, is_stop, reopen=False)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")

    for name, addr, size in regions:
        if is_stop and is_stop():
            break
        try:
            data = _read_span(fl, addr, size, is_stop)
            out["regions"][name] = {"addr": addr, "data": data, "error": None}
            on_stage("  %-13s 0x%06X +0x%03X -> %s%s"
                     % (name, addr, size, data[:16].hex(),
                        "..." if len(data) > 16 else ""))
        except Exception as ex:
            out["regions"][name] = {"addr": addr, "data": None, "error": str(ex)}
            on_stage("  %-13s 0x%06X refused (%s)" % (name, addr, ex))

    cs = out["regions"].get("coding_struct", {}).get("data")
    if cs and len(cs) > IMMO_STATUS_STRUCT_OFFSET:
        st = cs[IMMO_STATUS_STRUCT_OFFSET]
        out["immo_status"] = st
        out["immo_text"] = IMMO_STATUS_STATES.get(st, "unrecognised (0x%02X)" % st)
        on_stage("  -> immo status (struct+0x%02X = 0x%06X) = 0x%02X : %s"
                 % (IMMO_STATUS_STRUCT_OFFSET,
                    CODING_STRUCT_ADDR + IMMO_STATUS_STRUCT_OFFSET, st, out["immo_text"]))
        if len(cs) > K5_STRUCT_OFFSET + 1:
            out["k5"] = (cs[K5_STRUCT_OFFSET] << 8) | cs[K5_STRUCT_OFFSET + 1]
            on_stage("  -> K5 (struct+0x%02X) = 0x%04X" % (K5_STRUCT_OFFSET, out["k5"]))

    try:
        fl._try_quiet_stop()
    except Exception:
        pass

    if out["immo_status"] is not None:
        on_stage("RESULT: immo status 0x%02X (%s). Coding/immo image captured."
                 % (out["immo_status"], out["immo_text"]))
    else:
        on_stage("RESULT: coding struct not read -- see per-region status above.")
    return out


# Candidate ranges for the checksum-range probe.  0x31 C5 verifies its OWN
# descriptor-aligned ranges; the program BLOCK is 0x40000-0x17FFFF (self-header)
# but 0x31 C5 accepts only 0x40000-0x9FFFF and rejects the full block (7F 33 22),
# so the verifiable extent is finer than the block.  This probes what the ECU
# will actually checksum, especially the 0xA0000+ region (writable, but its
# verifiable sub-range is unknown).  All entries are (start, end_inclusive, label).
_CHECKSUM_PROBE_RANGES = [
    (0x040000, 0x09FFFF, "program 0x40000-0x9FFFF (ODIS; expect ACCEPT)"),
    (0x040000, 0x17FFFF, "program full 0x40000-0x17FFFF (expect REJECT)"),
    (0x0A0000, 0x17FFFF, "0xA0000-0x17FFFF"),
    (0x0A0000, 0x0FFFFF, "0xA0000-0xFFFFF"),
    (0x0A0000, 0x0BFFFF, "0xA0000-0xBFFFF"),
    (0x0A0000, 0x0BFEFF, "0xA0000-0xBFEFF"),
    (0x080000, 0x17FFFF, "0x80000-0x17FFFF"),
    (0x0C0000, 0x17FFFF, "0xC0000-0x17FFFF"),
    (0x0A0000, 0x0DFFFF, "0xA0000-0xDFFFF"),
]


def checksum_range_probe(fl, image=None, *, ranges=None, speed="Medium",
                         allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: find which [start,end] ranges the ECU's 0x31 C5 checksum
    routine accepts.  Opens the programming session (0x85 + LVL1) and, for each
    candidate range, sends 0x31 C5 <start> <end> <sum> and polls 0x33 C5, recording
    accepted (73 C5, valid range -- match or mismatch) vs rejected (7F 33 22 =
    conditionsNotCorrect, not a valid checksum range).  NOTHING is erased, written
    or transferred -- 0x31 C5 only computes/compares a sum.

    The checksum VALUE does not affect range-acceptance (a valid range with a wrong
    sum still returns 73 C5, just mismatch), so a real image is optional: if given,
    the correct sum is sent (so 00 = the ECU already holds that data); otherwise a
    dummy 0 is used and a valid range reports mismatch.

    Returns a list of (start, end, label, verdict)."""
    on_stage = on_stage or (lambda _m: None)
    if ranges is None:
        ranges = _CHECKSUM_PROBE_RANGES
    from .edc16 import fast_prime_from_image
    fip = fast_prime_from_image(image) if image is not None else False

    on_stage("Connecting + programming session (non-destructive)...")
    fl.open_program_session(speed, allow_fast, fip, is_stop, on_stage)
    on_stage("Session open. Probing 0x31 C5 ranges (no erase, no write)...")

    results = []
    for (start, end, label) in ranges:
        if is_stop and is_stop():
            break
        cks = 0
        if image is not None and end + 1 <= len(image):
            try:
                _, cks = encrypt(image, start, end + 1)
            except Exception:
                cks = 0
        try:
            verdict = fl._checksum_probe_classify(start, end, cks)
        except Exception as ex:
            verdict = "error:%s" % ex
        on_stage("  0x%06X-0x%06X  %-38s -> %s" % (start, end, label, verdict))
        results.append((start, end, label, verdict))

    try:
        fl._finalize()
    except Exception:
        pass
    accepted = [r for r in results if r[3].startswith("accepted")]
    on_stage("Done. %d/%d ranges accepted by the 0x31 C5 routine."
             % (len(accepted), len(results)))
    return results


_SEED_PROBE_SUBFUNCS = [0x01, 0x03, 0x05, 0x07, 0x09, 0x0B, 0x0D, 0x0F, 0x11, 0x13]


def security_seed_probe(fl, *, subfuncs=None, speed="Medium", allow_fast=True,
                        on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: enumerate the ECU's SecurityAccess levels.  Connects
    (default session) and, for each odd sub-function, sends 0x27 <sf> -- a seed
    request ONLY, never a key -- recording whether the ECU returns a seed (level
    exists) or NAKs (7F 27).  No key is sent, so nothing unlocks and nothing is
    written.  Known: 27 01/02 = write/SA2, 27 03/04 = read.  Any higher sub-function
    that returns a seed is a candidate level for the boot region (which the read
    gate refuses at the read/write levels).  Returns [(subfunc, verdict), ...]."""
    on_stage = on_stage or (lambda _m: None)
    if subfuncs is None:
        subfuncs = _SEED_PROBE_SUBFUNCS
    on_stage("Connecting (default session; non-destructive)...")
    fl.connect()
    on_stage("Requesting seeds (no keys sent)...")
    results = []
    for sf in subfuncs:
        if is_stop and is_stop():
            break
        try:
            v = fl._seed_probe_one(sf)
        except Exception as ex:
            v = "error:%s" % ex
        known = {0x01: " (write/SA2)", 0x03: " (read)"}.get(sf, "")
        on_stage("  27 %02X%s -> %s" % (sf, known, v))
        results.append((sf, v))
    try:
        fl._finalize()
    except Exception:
        pass
    got = [r for r in results if r[1].startswith("seed") or r[1].startswith("open")]
    on_stage("Done. %d of %d sub-functions are live security levels."
             % (len(got), len(results)))
    return results


def read_probe(fl, *, include_program=True, include_mpc=False, speed="Medium",
               allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE read-grant probe: open ONE read (0x86) session, then for
    each enabled range issue requestUpload(0x35) and report whether the ECU grants
    it (02 75 FF).  The read-side analog of the write dry run.  Answers 'can the
    program area / MPC be READ over OBD?' without transferring the data.

    ONE session for all ranges (a fresh session per range re-inited the link
    after security access -- Bear's report); between ranges the pending upload is
    closed with transferExit(0x37) so the next requestUpload starts clean.
    Returns {name: bool}."""
    on_stage = on_stage or (lambda _m: None)
    ranges = []
    if include_program:
        ranges.append(("program", B.PROGRAM_START, B.PROGRAM_END - B.PROGRAM_START))
    if include_mpc:
        ranges.append(("mpc", B.MPC_ADDR, B.MPC.writes[0].size))
    results = {}
    on_stage("Opening read session (%s)..." % fl.bus_name())
    fl.open_read_session(speed, allow_fast, is_stop, reopen=False)
    on_stage("Read session open (0x86 + read security).")
    for i, (name, addr, size) in enumerate(ranges):
        if is_stop and is_stop():
            raise StopRequested("read-probe stopped")
        on_stage("RequestUpload %s @0x%06X size=0x%X (0x35)..." % (name, addr, size))
        try:
            fl._request_upload(addr, size)
            on_stage("  -> 0x35 ACCEPTED (02 75 FF): %s IS readable over OBD" % name)
            results[name] = True
            try:
                fl._request_transfer_exit()          # close the granted upload (0x37)
                on_stage("  -> upload closed (transferExit).")
            except Exception as ex:
                on_stage("  -> (transferExit after grant not clean: %s)" % ex)
        except Exception as ex:
            on_stage("  -> 0x35 REFUSED: %s (%s not readable this way)" % (ex, name))
            results[name] = False
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    on_stage("Read-probe done: %s"
             % ", ".join("%s=%s" % (k, "READABLE" if v else "refused")
                         for k, v in results.items()))
    return results
