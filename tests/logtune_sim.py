# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A small, deterministic EDC16 'car' that writes KWHack-layout data logs.

It reads the loaded calibration the way the ECU would (target map blend by
atmospheric pressure, torque limit -> quantity via FMTC) and adds the physics
the tests need to have a known right answer:

* the boost actuator needs ``pilot + need_offset(rpm)`` duty to hold the
  setpoint -- so the pilot learner should move cells by ``gain x offset``;
* air mass follows boost, so full-load lambda is rich at low rpm where the
  torque limit asks for more fuel than the air supports;
* the smoke-limit torque is reported far above everything (the placeholder
  smoke table limits nothing), matching the calibration under test.

Logs are laid out on the calibration's real measuring blocks, so column
identities resolve through ChannelNamer exactly as a real log would.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from ovtsuite.datalog import logfile as lf
from ovtsuite.logtune import grid
from ovtsuite.logtune.roles import ROLES, role_of_signal


def need_offset(rpm):
    """Extra duty the (simulated) turbo needs over the stock pilot."""
    return 5.0 if 1900 <= rpm <= 3100 else 0.0


class Car:
    def __init__(self, session, atm=1010.0):
        self.s = session
        self.E = {e.name: e for e in session.entities}
        self.atm = atm

    def target(self, rpm, iq):
        bas, bas2 = self.E["PCR_pBDesBas_MAP"], self.E["PCR_pBDesBas2_MAP"]
        f = grid.lookup(self.E["PCR_facDesBas_CUR"], self.atm)
        sp = f * grid.lookup(bas, rpm, iq) + (1 - f) * grid.lookup(bas2, rpm, iq)
        cap = min(self.E["PCR_pBDesMax_C"].cell(0, 0),
                  grid.lookup(self.E["PCR_pBDesMaxAP_MAP"], rpm, self.atm))
        return min(sp, cap)

    def pilot(self, rpm, iq):
        f = grid.lookup(self.E["PCR_facCtlBas_CUR"], self.atm)
        return (f * grid.lookup(self.E["PCR_rBPCtlBas_MAP"], rpm, iq)
                + (1 - f) * grid.lookup(self.E["PCR_rBPCtlBas2_MAP"], rpm, iq))

    def torque_limit(self, rpm):
        return grid.lookup(self.E["EngPrt_trqLimP_MAP"], self.atm, rpm)

    def iq_for(self, rpm, nm):
        return grid.lookup(self.E["FMTC_trq2qBas_MAP"], rpm, nm)


def _cells(namer, groups):
    out = []
    for g in groups:
        for f in range(namer.slots(g)):
            label, sig, unit = namer.resolve(g, f)
            out.append((g, f, label, sig, unit))
    return out


def write_log(path, session, namer, groups, rows):
    """``rows``: list of (t_seconds, {role: value}) -> KWHack CSV on ``groups``."""
    cells = _cells(namer, groups)
    chans = [lf.Channel(g, f, label, unit, sig) for g, f, label, sig, unit in cells]
    t0 = datetime(2026, 9, 30, 12, 0, 0)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = lf.KwhackWriter(fh, chans)
        for t, vals in rows:
            texts = []
            for g, f, label, sig, unit in cells:
                role = role_of_signal(sig)
                v = vals.get(role) if role else None
                texts.append("N/A" if v is None else ("%.2f %s" % (v, unit)).strip())
            w.row(t0 + timedelta(seconds=t), texts)
    return path


def wot_pull(car, t0=0.0, r0=1300.0, r1=4500.0, secs=8.0, hz=10.0, overshoot=0.0,
             clt=90.0, smoke_torque=5000.0, eoi=6.0, egt=None):
    """``eoi``: end of injection the logged SOI/duration imply (deg ATDC).
    ``egt``: None (not logged), a number (a stuck substitute) or a callable of rpm."""
    rows, n = [], int(secs * hz)
    for k in range(n + 1):
        t = t0 + k / hz
        rpm = r0 + (r1 - r0) * k / n
        nm = car.torque_limit(rpm)
        iq = car.iq_for(rpm, nm)
        sp = car.target(rpm, iq)
        rise = min(1.0, (k / hz) / 1.2)                   # boost builds over 1.2 s
        bump = overshoot * math.exp(-((rpm - 2100) / 180.0) ** 2)
        boost = car.atm + (sp - car.atm) * rise + bump
        settled = rise >= 1.0 and bump < 20
        # The governor on top of the pilot: pushing while boost is short,
        # backing off through an overshoot, holding the needed offset once settled.
        gov = 8.0 if boost < sp - 50 else (-6.0 if boost > sp + 50 else 0.0)
        duty = car.pilot(rpm, iq) + (need_offset(rpm) if settled else gov)
        ve = max(0.6, 1.0 - ((rpm - 2600.0) / 3200.0) ** 2)
        maf = (380.0 + 0.42 * (boost - car.atm)) * ve
        soi = grid.lookup(car.E["InjCrv_phiBas9_GMAP"], rpm, iq)
        e_t = egt(rpm) if callable(egt) else egt
        rows.append((t, {"soi": soi, "dur": soi + eoi, "egt": e_t,
                         "rpm": rpm, "iq": iq, "boost": boost, "boost_spec": sp, "n75": duty,
                         "atm": car.atm, "pedal": 100.0, "clt": clt, "maf": maf,
                         "trq_drv": 460.0, "trq_lim": nm, "trq_smk": smoke_torque,
                         "trq_final": nm, "trq_gbx": 1000.0, "iat": 25.0}))
    return rows


def cruise(car, speeds=(60, 80, 100, 120), secs=30.0, hz=5.0, iq_scale=1.0, t0=0.0):
    rows, t = [], t0
    for v in speeds:
        rpm = 1200 + 13 * v
        iq = (6.0 + 0.0009 * v * v) * iq_scale
        for k in range(int(secs * hz)):
            rows.append((t, {"rpm": rpm, "iq": iq, "vss": float(v), "gear": 6.0, "pedal": 20.0,
                             "clt": 88.0, "maf": 450.0, "maf_spec": 455.0}))
            t += 1.0 / hz
        t += 2.0
    return rows
