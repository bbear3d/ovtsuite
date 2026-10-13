# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Selective output tests (Stellglied / actuator tests) of a binary.

The ECU stores the set of output tests it supports as ``TstTog_Code_C`` -- an
array of 16-bit tester IDs -- paired index-for-index with ``TstTog_Demand_C``,
a signal number that the firmware resolves through the ``SigTstName`` value
table to the actual actuator. The tester ID differs between the P350 (038) and
P475 (03G) platforms, but the ``SigTst_*`` signal is the invariant, so meanings
are keyed off the signal, not the raw ID.

``output_tests(session)`` returns one dict per supported test:
``{code, code_hex, sig, meaning, demand}``. It locates the table from the
knowledge base when possible and falls back to a structural scan (for datasets
whose XDF omits the ``TstTog_*`` symbols, e.g. the P475 8.4 Audi A6 files), so
it is dynamic per binary -- no per-file table needs to be hand-maintained.
"""
from typing import List

# SigTstName value tables, baked from the reference A2Ls (no A2L at runtime).
_SIG_P350 = {
    0: "AC", 1: "ADP", 2: "AFV", 3: "AOHtCDHt1", 4: "AOHtCDHt2",
    5: "AltRLYExc", 6: "BPA", 7: "BPA2", 8: "CtAp", 9: "CtT", 10: "EGR",
    11: "EGRClgBypVlv", 12: "ENGMENG", 13: "ENGMGEAR", 14: "Fan1", 15: "Fan2",
    16: "FlPpCD", 17: "GLWLAMP", 18: "GLWRLY", 19: "GLWRLYGSK3", 20: "LSUCDHt0",
    21: "MILLAMP", 22: "PFltRLYHtg", 23: "PSP", 24: "SYSLAMP",
    25: "StrtCDRLYOffRly", 26: "StrtCDRLYOnRly", 27: "TVA", 28: "VSA",
    255: "unused",
}
_SIG_P475 = {
    0: "AC", 1: "AFV", 2: "AOHtCDHt1", 3: "AOHtCDHt2", 4: "AltRLYExc",
    5: "BPA", 6: "BPA2", 7: "CtAp", 8: "CtT", 9: "EGR", 10: "EGRClgBypVlv",
    11: "ENGMENG", 12: "ENGMENG1", 13: "ENGMENG2", 14: "ENGMGEAR",
    15: "ENGMGEAR1", 16: "ENGMGEAR2", 17: "EngM", 18: "Fan1", 19: "Fan2",
    20: "FlClgCD", 21: "FlPpCD", 22: "GLWLAMP", 23: "GLWRLY", 24: "GLWRLYGSK3",
    25: "LSUCDHt0", 26: "MILLAMP", 27: "PFltLmp", 28: "PFltRLYHtg", 29: "PSP",
    30: "SYSLAMP", 31: "StrtCDRLYOffRly", 32: "StrtCDRLYOnRly", 33: "TVA",
    34: "VSA", 35: "WDAL", 255: "unused",
}
# SigTst_* signal -> plain-English actuator. Marked confidence: the ECU symbol
# is authoritative; the plain name is a reading (best-effort for the '?').
_MEAN = {
    "EGR": "EGR valve", "AC": "A/C compressor (intervention/clutch)",
    "BPA": "N75 valve - boost-pressure actuator",
    "BPA2": "Boost-pressure actuator 2 (2nd N75)",
    "TVA": "Intake flap - throttle-valve actuator",
    "GLWLAMP": "Glow-plug indicator light",
    "MILLAMP": "Check-engine light (MIL)", "Fan1": "Cooling fan 1",
    "Fan2": "Cooling fan 2",
    "GLWRLYGSK3": "Glow-plug control module (GSK3 relay)",
    "GLWRLY": "Glow-plug relay (main)",
    "AOHtCDHt1": "Low-output coolant heater relay",
    "AOHtCDHt2": "High-output coolant heater relay",
    "EGRClgBypVlv": "EGR-cooler bypass valve",
    "VSA": "Variable-swirl / intake-swirl actuator",
    "AFV": "Anti-shudder / intake air-flap valve",
    "AltRLYExc": "Alternator excitation (field) relay",
    "PFltLmp": "DPF (particulate filter) warning lamp",
    "PFltRLYHtg": "DPF heater relay",
    "ENGMENG": "Engine-mgmt engine-torque CAN output",
    "ENGMENG1": "Engine-mgmt engine output 1",
    "ENGMENG2": "Engine-mgmt engine output 2",
    "ENGMGEAR": "Engine-mgmt gearbox-torque CAN output",
    "ENGMGEAR1": "Engine-mgmt gearbox output 1",
    "ENGMGEAR2": "Engine-mgmt gearbox output 2",
    "EngM": "Engine-management output",
    "LSUCDHt0": "Lambda (LSU) sensor heater",
    "PSP": "Pre-supply / fuel-transfer pump", "FlPpCD": "Fuel pump",
    "FlClgCD": "Fuel-cooling pump/valve",
    "SYSLAMP": "System / general warning lamp",
    "StrtCDRLYOnRly": "Starter relay - engage",
    "StrtCDRLYOffRly": "Starter relay - release",
    "CtAp": "Coolant after-run pump",
    "CtT": "Map-controlled thermostat heater",
    "ADP": "Adaptation actuator",
    "WDAL": "Warning-display / watchdog lamp", "unused": "(unused slot)",
}

# Confidence of the plain-English reading, keyed off the invariant SigTst_*
# signal. "confirmed" == cross-checked against the known tester-ID list;
# "best-effort" == the ECU symbol is authoritative but the plain name is a
# reading. Anything not listed defaults to "inferred" (symbol-derived, high
# confidence). Keying off the signal keeps this dynamic: a new binary reusing
# a known signal inherits the right flag automatically.
_CONF = {
    "EGR": "confirmed", "AC": "confirmed", "BPA": "confirmed",
    "TVA": "confirmed", "GLWLAMP": "confirmed", "MILLAMP": "confirmed",
    "Fan1": "confirmed", "Fan2": "confirmed", "GLWRLYGSK3": "confirmed",
    "AOHtCDHt1": "confirmed", "AOHtCDHt2": "confirmed",
    "VSA": "best-effort", "ENGMENG": "best-effort", "ENGMENG1": "best-effort",
    "ENGMENG2": "best-effort", "ENGMGEAR": "best-effort",
    "ENGMGEAR1": "best-effort", "ENGMGEAR2": "best-effort",
    "EngM": "best-effort", "ADP": "best-effort",
}

# actuators expected in any output-test table, used to validate a scan location
_CORE = {9, 5, 18}   # EGR, BPA, Fan1 (same index in both platforms)


def _sig_table(family):
    return _SIG_P350 if family == "p350" else _SIG_P475


def _u16(d, o):
    return (d[o] << 8) | d[o + 1]


def _locate(session):
    """(code_addr, demand_addr, count) for TstTog_Code_C / TstTog_Demand_C.

    Prefers the knowledge-base match; falls back to a structural scan of the
    calibration region for the paired uint16 code array + uint8 SigTstName-index
    array, so a dataset whose XDF omits the symbols is still handled."""
    ents = {e.name: e for e in
            list(session.entities) + list(session.variant_entities())}
    ce, de = ents.get("TstTog_Code_C"), ents.get("TstTog_Demand_C")
    if ce is not None and de is not None:
        n = max(1, (de.addr - ce.addr) // 2)
        return ce.addr, de.addr, n
    d = bytes(session.binfile.data)
    hi = len(d)
    sig = _sig_table(session.family)
    valid = set(sig)
    for n in (35, 34, 33, 32, 31, 30, 29, 28):
        for x in range(0x1E0000, min(hi, 0x200000) - 3 * n, 2):
            code = [_u16(d, x + 2 * j) for j in range(n)]
            dem = d[x + 2 * n:x + 3 * n]
            pairs = []
            ok = True
            for j in range(n):
                c, m = code[j], dem[j]
                cgood = c not in (0, 0xFFFF) and c <= 0x4FFF
                if m in valid and m != 255 and cgood:
                    pairs.append(m)
                elif c in (0, 0xFFFF) and m == 0xFF:
                    pass
                else:
                    ok = False
                    break
            if ok and len(pairs) >= 10 and len(set(pairs)) >= 9 \
                    and _CORE.issubset(set(pairs)):
                return x, x + 2 * n, n
    return None, None, 0


def output_tests(session) -> List[dict]:
    """Every selective output test the binary supports (see module docstring).

    EDC15 sessions are served by ``edc15.outtest15`` (xcwStell table).

    Each dict also carries ``code_addr`` (address of the ``TstTog_Code_C`` array
    the tests were read from) and ``confidence`` (of the plain-name reading), so
    a renderer can show the source location and a per-row confidence flag
    without any per-file table.
    """
    if getattr(session, "is_edc15", False):
        from .edc15.outtest15 import as_dicts
        return as_dicts(session)
    ca, da, n = _locate(session)
    if ca is None:
        return []
    d = bytes(session.binfile.data)
    sig = _sig_table(session.family)
    out = []
    for j in range(n):
        code = _u16(d, ca + 2 * j)
        dem = d[da + j] if da + j < len(d) else 0xFF
        if code in (0, 0xFFFF) or dem not in sig or dem == 255:
            continue
        name = sig[dem]
        out.append(dict(code=code, code_hex="0x%04X" % code, demand=dem,
                        sig="SigTst_" + name, meaning=_MEAN.get(name, name),
                        code_addr=ca, confidence=_CONF.get(name, "inferred")))
    return out
