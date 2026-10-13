# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which logged cell is which physical quantity.

A log column is identified by the A2L measurement it carries, never by its
display label: ``Eng_nAvrg`` is engine speed whatever a scan tool calls the
column. The measurement name is taken, in order of trust, from

1. ``Channel.signal`` (set when the log was recorded against a binary),
2. the loaded binary's measuring-block map -- ``UACC_RSigBlkGrp_MAP`` cell ->
   ESB -> signal catalogue, via :class:`~ovtsuite.datalog.channels.ChannelNamer`,
   which is what makes a plain VCDS "Group 011 / Field 3" column resolvable,
3. an A2L-shaped symbol in parentheses in the column label, the way OVT-Suite
   and KWHack label their own logs ("Engine speed (Eng_nAvrg)").

The same tables drive the logging presets: a recipe names the roles an
analysis needs, and :func:`plan_groups` picks the fewest measuring blocks in
*this* binary that carry them, so a preset follows whatever group layout the
calibration actually has.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: role -> A2L measurements that carry it, most specific first.
ROLES: Dict[str, Tuple[str, ...]] = {
    "rpm": ("Eng_nAvrg",),
    "iq": ("InjCtl_qSetUnBal", "InjCtl_qSet"),
    "maf": ("AFSCD_mAirPerCyl",),
    "maf_spec": ("AirCtl_mDesVal",),
    "boost": ("BPSCD_pFltVal", "BPSCD_pVal"),
    "boost_spec": ("PCR_pBDes",),
    "n75": ("PCR_rBPACD",),
    "atm": ("APSCD_pVal",),
    "pedal": ("APPCD_rAPP1UnFlt", "AccPed_rChkdVal"),
    "soi": ("InjCrv_phiMI1Des",),
    "dur": ("InjVlv_phiMI1DurCorr",),
    "trq_drv": ("AccPed_trqInrDesSig",),
    "trq_lim": ("EngPrt_trqLim",),
    "trq_smk": ("FMTC_trqInrLimSmk",),
    "trq_final": ("CoEng_trqInrLim",),
    "trq_gbx": ("Gearbx_trqDec",),
    "trq_cruise": ("CrCtl_trqDes",),
    # The VEHICLE-DYNAMICS torque interventions, both directions -- NOT the
    # gearbox's. The A2L is explicit: CoVM_trqInc is "Momenteneingriff von
    # MSR" (engine drag-torque control) and CoVM_trqDec "von ASR" (traction
    # control), so both come from the ESP, not the EGS. A gearbox downshift
    # blip would NOT appear here; it travels the AST speed-request path, which
    # has no measuring descriptor at all. Worth logging anyway, because MSR is
    # the other thing that raises engine torque on a trailing throttle, and
    # ruling it out is worth four cells.
    #
    # Both cells use KWP display formula 94, which decode_cell does not
    # implement, so they log as the raw triplet "(94 a b)". Rest values on the
    # P475 LQ: (94 100 255) for the ASR reduction, (94 100 0) for the MSR
    # increase -- max and min of their ranges, i.e. nothing requested.
    "trq_inc": ("CoVM_trqInc",),
    "trq_dec_asr": ("CoVM_trqDec",),
    # Gearbox-intervention status flags, for whether anything was asked at all.
    "st_tsc": ("Gearbx_stTSC_mp",),
    "st_dcs": ("FrmMng_stDCS",),
    "st_vdyn": ("CoVM_stActDem",),
    "st_can": ("NetMng_stCAN",),
    "trq_inr": ("FMTC_trqInr",),
    "fuel_lph": ("CoEng_dvolFlConsum",),
    "vss": ("VSSCD_v",),
    "gear": ("Gearbx_stGear",),
    "iat": ("IATSCD_tAir",),
    "clt": ("CTSCD_tClnt",),
    "tfuel": ("FTSCD_tFuel",),
    "egr": ("AirCtl_rEGR",),
    "egt": ("EGPpCD_tPreTrbn",),
    "egt_model": ("EGSys_tEGLSU",),
    "lambda": ("LSU_rLmbd",),
}

ROLE_LABELS: Dict[str, str] = {
    "rpm": "Engine speed", "iq": "Injected quantity", "maf": "Air mass (actual)",
    "maf_spec": "Air mass (specified)", "boost": "Boost (actual)",
    "boost_spec": "Boost (specified)", "n75": "Boost actuator duty",
    "atm": "Atmospheric pressure", "pedal": "Accelerator pedal",
    "soi": "Start of injection", "dur": "Injection duration",
    "trq_drv": "Driver's-wish torque", "trq_lim": "Protection torque limit",
    "trq_smk": "Smoke-limit torque", "trq_final": "Final torque limit",
    "trq_gbx": "Gearbox torque reduction", "trq_inr": "Inner torque",
    "trq_inc": "Torque increase intervention (MSR, from ESP)",
    "trq_dec_asr": "Torque reduction intervention (ASR, from ESP)",
    "trq_cruise": "Cruise-control torque request",
    "st_tsc": "Gearbox intervention active (TSC/EGS)",
    "st_dcs": "Drag-torque intervention (MSR)",
    "st_vdyn": "Vehicle-dynamics intervention",
    "st_can": "CAN controller status",
    "fuel_lph": "Fuel consumption", "vss": "Vehicle speed", "gear": "Gear",
    "iat": "Intake air temp", "clt": "Coolant temp", "tfuel": "Fuel temp",
    "egr": "EGR duty", "egt": "Pre-turbine temp (sensor)",
    "egt_model": "Exhaust temp model (sensor position)", "lambda": "Lambda (sensor)",
}

#: Transmission-controller roles. The GS19's blocks are fixed by the
#: controller rather than described in the calibration, so these map onto the
#: slot keys in :mod:`ovtsuite.zf6hp.msblock6hp`. They share this vocabulary
#: with the engine roles above so one planner serves both controllers; the
#: ``Tcu_`` prefix is what keeps the two namespaces from ever colliding.
TCU_ROLES: Dict[str, Tuple[str, ...]] = {
    "tcu_rpm": ("Tcu_nEng",), "tcu_nturb": ("Tcu_nTurb",),
    "tcu_nout": ("Tcu_nOut",), "tcu_gear": ("Tcu_stGear",),
    "tcu_pedal": ("Tcu_rPed",), "tcu_vss": ("Tcu_vVeh",),
    "tcu_atf": ("Tcu_tAtf",), "tcu_lever": ("Tcu_stLever",),
    "tcu_tcc": ("Tcu_stTcc",), "tcu_slip": ("Tcu_nTccSlip",),
    "tcu_prog": ("Tcu_stProg",), "tcu_dsp": ("Tcu_stDsp",),
    "tcu_trq": ("Tcu_trqEng",), "tcu_trq_max": ("Tcu_trqEngMax",),
    "tcu_kick": ("Tcu_stKickdown",), "tcu_accel": ("Tcu_aVeh",),
    "tcu_tcamp": ("Tcu_facTcAmp",), "tcu_mode": ("Tcu_stMode",),
    "tcu_tmod": ("Tcu_tModule",),
    "tcu_pcv1": ("Tcu_iPcv1",), "tcu_pcv2": ("Tcu_iPcv2",),
    "tcu_pcv3": ("Tcu_iPcv3",), "tcu_pcv4": ("Tcu_iPcv4",),
    "tcu_pcv5": ("Tcu_iPcv5",), "tcu_pcv6": ("Tcu_iPcv6",),
    # switches, supplies and the ECU-sourced identity fields. No preset needs
    # them today, but every named slot gets a role so a log column is always
    # identified rather than falling through as "Group N / Cell M".
    "tcu_brake": ("Tcu_stBrake",), "tcu_shiftlock": ("Tcu_stShiftLock",),
    "tcu_u_valves": ("Tcu_uValves",), "tcu_f125": ("Tcu_stF125",),
    "tcu_obd": ("Tcu_stObd",), "tcu_n88": ("Tcu_stN88",),
    "tcu_tip_rec": ("Tcu_stTipRec",), "tcu_tip_dir": ("Tcu_stTipDir",),
    "tcu_tip_btn": ("Tcu_stTipBtn",), "tcu_trq_nom": ("Tcu_trqEngNom",),
    "tcu_eng_var": ("Tcu_stEngVar",), "tcu_trn_var": ("Tcu_stTrnVar",),
    "tcu_can_lvl": ("Tcu_stCanLvl",), "tcu_u_t30": ("Tcu_uTerm30",),
    "tcu_u_t15": ("Tcu_uTerm15",), "tcu_can_t15": ("Tcu_stCanT15",),
    "tcu_afterrun": ("Tcu_stAfterrun",), "tcu_thermal": ("Tcu_stThermal",),
}

TCU_ROLE_LABELS: Dict[str, str] = {
    "tcu_rpm": "Engine speed (TCU)", "tcu_nturb": "Turbine speed (G182)",
    "tcu_nout": "Output shaft speed (G195)", "tcu_gear": "Engaged gear",
    "tcu_pedal": "Accelerator pedal (TCU)", "tcu_vss": "Vehicle speed (TCU)",
    "tcu_atf": "Fluid temperature (G93)", "tcu_lever": "Selector lever",
    "tcu_tcc": "Converter clutch status", "tcu_slip": "Converter slip",
    "tcu_prog": "Current shift program", "tcu_dsp": "Dynamic shift program",
    "tcu_trq": "Engine torque (TCU)", "tcu_trq_max": "Maximum engine torque",
    "tcu_kick": "Kick-down switch", "tcu_accel": "Acceleration",
    "tcu_tcamp": "Converter amplification", "tcu_mode": "Driving mode",
    "tcu_tmod": "Control module temperature",
    "tcu_pcv1": "Pressure control valve 1 (N215)",
    "tcu_pcv2": "Pressure control valve 2 (N216)",
    "tcu_pcv3": "Pressure control valve 3 (N217)",
    "tcu_pcv4": "Pressure control valve 4 (N218)",
    "tcu_pcv5": "Pressure control valve 5 (N233)",
    "tcu_pcv6": "Pressure control valve 6 (N371)",
    "tcu_brake": "Brake light switch", "tcu_shiftlock": "Shift lock solenoid (N110)",
    "tcu_u_valves": "Supply voltage (valves)", "tcu_f125": "Multi-function switch (F125)",
    "tcu_obd": "OBD info", "tcu_n88": "Solenoid valve 1 (N88)",
    "tcu_tip_rec": "Tiptronic switch recognition (F189)",
    "tcu_tip_dir": "Tiptronic switch up/down (F189)",
    "tcu_tip_btn": "Tiptronic buttons and paddles",
    "tcu_trq_nom": "Nominal engine torque", "tcu_eng_var": "Engine variant (ECU signal)",
    "tcu_trn_var": "Transmission variant (ECU signal)",
    "tcu_can_lvl": "CAN software level (ECU signal)",
    "tcu_u_t30": "Supply voltage (terminal 30)", "tcu_u_t15": "Supply voltage (terminal 15)",
    "tcu_can_t15": "CAN information (terminal 15)",
    "tcu_afterrun": "Control module afterrun", "tcu_thermal": "Thermal protection",
}

ROLES.update(TCU_ROLES)
ROLE_LABELS.update(TCU_ROLE_LABELS)

_SIGNAL_ROLE: Dict[str, str] = {sig: role for role, sigs in ROLES.items() for sig in sigs}
_SYMBOL_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\)")


@dataclass(frozen=True)
class Recipe:
    key: str
    title: str
    needs: Tuple[str, ...]                 # roles the analysis cannot run without
    wants: Tuple[str, ...] = ()            # roles that sharpen it
    how: str = ""                          # how to drive while logging
    #: Most blocks this preset may tick. The controller answers one block per
    #: round trip, so every extra block costs sample rate -- and for a shift
    #: point, rate IS the measurement. Engine recipes keep the old ceiling.
    max_groups: int = 6


RECIPES: Tuple[Recipe, ...] = (
    Recipe("boost", "Boost control (target + actuator pilot)",
           ("rpm", "iq", "boost", "boost_spec", "n75"), ("atm", "pedal", "clt"),
           "Coolant at 70 deg C or more. Tiptronic, hold 3rd: 3 full-throttle pulls from "
           "1300 rpm to 4500 rpm, then the same in 4th. Add 2-3 minutes of "
           "steady part-throttle driving at 1800-3000 rpm (constant speed, "
           "gentle hills) so the controller settles in the cells cruise uses."),
    Recipe("fuel", "Full-load fuelling and active limiter",
           ("rpm", "iq", "maf", "pedal"),
           ("trq_drv", "trq_lim", "trq_smk", "trq_final", "trq_gbx", "clt", "boost", "egt"),
           "Coolant at 70 deg C or more. Tiptronic, hold 3rd: full throttle from 1300 rpm "
           "to the limiter, twice; repeat in 4th. Let the intercooler cool "
           "between runs."),
    Recipe("timing", "Injection timing (start + duration)",
           ("rpm", "iq", "soi", "dur"), ("clt", "tfuel", "egt"),
           "Coolant at 70 deg C or more. Same full-throttle pulls as the fuelling log, "
           "plus a few seconds at steady 2000/2500/3000 rpm part throttle."),
    Recipe("egt", "Exhaust temperature (pre-turbine sensor)",
           ("rpm", "iq", "egt"), ("pedal", "maf", "clt"),
           "Coolant at 70 deg C or more, tiptronic in 4th: a full-throttle pull from 1500 rpm "
           "to the limiter, then hold full throttle up a long grade if you have one."),
    Recipe("warmup", "Warm-up running (coolant below 70 deg C)",
           ("rpm", "iq", "clt", "soi", "dur"), ("pedal", "maf"),
           "Start from a cold engine and log without stopping until coolant passes 75 deg C. "
           "Drive normally, with steady part-throttle stretches at 1500-2500 rpm and a few firm "
           "accelerations in 3rd from 1300 rpm in each coolant band; add full-throttle ones once "
           "you're happy the oil is warm enough. The report splits the log where the "
           "calibration switches timing map (on this tune about 21, 46 and 61 deg C)."),
    Recipe("coldtakeoff", "Cold take-off (active limiter, boost control, intake air)",
           ("rpm", "iq", "clt", "trq_drv", "trq_lim", "trq_smk",
            "boost", "boost_spec", "n75", "iat"),
           (),
           "Start logging from a cold start (coolant under 40 deg C) and keep going until "
           "it passes 70. In D, several take-offs from a full stop -- some at half pedal, "
           "some firm -- with a few seconds at idle in gear before each, then the same take-offs "
           "again once warm for comparison. The log shows which limit holds the fuel "
           "(driver's wish, protection limit or smoke limit), the boost target and actuator "
           "duty from idle through the launch, and the intake-air temperature the smoke "
           "limit's pressure correction uses.",
           max_groups=4),
    Recipe("gbxintv", "Gearbox torque intervention (ECU side)",
           # trq_inc is the ESP's drag-torque (MSR) increase, not the
           # gearbox's -- adding it was a correction to a wrong claim, not the
           # answer to the downshift-blip question. It earns its place by
           # ruling MSR out: on the 2026-10-08 dual log it never left its rest
           # value in 9130 samples. The gearbox's own increase is the AST
           # speed request, which no block can carry.
           ("trq_gbx", "trq_inc", "st_tsc", "st_dcs"),
           # gear is deliberately NOT wanted. It lives only in groups 133 and
           # 213, and 213 returned N/A for all four cells on every one of 3377
           # samples on the P475 LQ: the calibration's group map DEFINING a
           # group is not the ECU SERVING it. Leaving gear out lets the
           # low-number tiebreak in plan_groups land pedal and road speed on
           # group 6, which is in the range this ECU does answer -- and the
           # engaged gear is anyway derivable from engine speed over road
           # speed against the six known ratios.
           # Pedal and road speed are still wanted for an engine-only log, but
           # with dual logging they come off the TCU side at full rate, so they
           # lose to the needs when the cap binds.
           ("rpm", "trq_dec_asr", "trq_inr", "trq_final", "st_vdyn",
            "pedal", "vss"),
           "For deciding whether a downshift blip is asked for at all. The ECU "
           "has no measuring descriptor for the INCREASING gearbox "
           "intervention -- only the decreasing one -- so the question is "
           "answered by the status flags: Gearbox intervention active says the "
           "gearbox asked for something, Drag-torque intervention says the MSR "
           "path engaged, and Gearbox torque reduction says what arrived was a "
           "cut rather than a blip.\n\n"
           "Coolant at 70 deg C or more. A dozen closed-throttle coast-downs "
           "through every gear, foot fully off -- these are the events that "
           "matter and they only exist at zero pedal, so do not feather the "
           "throttle to smooth them out. Then half a dozen kickdown downshifts "
           "from steady cruise in 4th and 5th for contrast, since those are the "
           "ones that do work. Leave a few seconds of steady driving between "
           "events so each one has a baseline in front of it.\n\n"
           "Capped at three blocks: a coasting downshift is over in a few "
           "tenths, and a status flag that is only sampled twice across it "
           "cannot be told from one that never set.",
           max_groups=3),
    Recipe("cruise", "Cruise efficiency (before/after comparison)",
           ("rpm", "iq", "vss"), ("gear", "pedal", "fuel_lph", "clt", "maf", "maf_spec"),
           "Coolant at 70 deg C or more, flat road, no wind if possible. Hold 60, 80, 100 "
           "and 120 km/h for at least 30 s each, same route and direction for "
           "the before and after logs."),
)


#: Transmission presets. Resolved against :class:`ovtsuite.zf6hp.msblock6hp.Blocks6hp`
#: rather than a calibration's block map, but by the same ``recipe_groups``.
#:
#: The GS19 answers about two and a half groups a second, so these are kept to
#: the fewest blocks that carry the question -- a preset that ticks five groups
#: measures a shift once every two seconds, which is no measurement at all.
TCU_RECIPES: Tuple[Recipe, ...] = (
    Recipe("shift", "Shift points (engaged gear + output speed)",
           ("tcu_gear", "tcu_nout", "tcu_pedal"),
           ("tcu_rpm", "tcu_nturb", "tcu_prog", "tcu_vss"),
           "Fluid at 60 deg C or more. Hold the pedal genuinely still -- 20, 30, 40 "
           "then 60 % -- and let the car accelerate up through the gears without "
           "moving your foot. A shift taken while lifting measures nothing, because "
           "the pedal crossed the threshold rather than road speed. Two runs per "
           "pedal, then coast to a stop in gear for the downshifts.",
           max_groups=2),
    Recipe("discover", "Shift map discovery (which schedule a program reads)",
           ("tcu_nturb", "tcu_nout", "tcu_prog"),
           # pedal is deliberately NOT wanted: a probe ladder is flat, so pedal
           # position cannot move a shift point, and wanting it pulls the cover
           # onto group 2 -- which duplicates output shaft and engaged gear off
           # group 1 while losing road speed and the dynamic program. Group 16
           # brings pedal along anyway.
           ("tcu_gear", "tcu_vss", "tcu_dsp"),
           "For driving a probe calibration whose ladders carry flat signatures "
           "(tools/zf6hp_tdi_retune/shift_map_probe.py). One unhurried pull per "
           "selector position, D then S, each taken far enough to reach 4th -- the "
           "class is read from the 2>3 speed AND the 3>4 speed together, and the "
           "highest-numbered class does not leave 3rd until 86 km/h. Pedal position "
           "does not matter: a probe ladder is flat, so there is no threshold to "
           "cross by moving your foot, which is the whole reason this is a separate "
           "preset from the shift-point one. Watch that the program number holds "
           "for the whole pull -- if it switches mid-pull the schedule switches "
           "with it and the run is void.",
           max_groups=2),
    Recipe("drvtype", "Driver-type downshift offset (pedal-triggered downshifts)",
           # nturb forces group 1, brake forces 3, mode forces 8 -- and those
           # three are the whole cover, so the plan is exact rather than lucky.
           ("tcu_nturb", "tcu_nout", "tcu_brake", "tcu_mode"),
           ("tcu_gear", "tcu_pedal", "tcu_kick", "tcu_vss", "tcu_rpm"),
           "For driving UKSVF_NABKF_<pair>RS probes "
           "(tools/zf6hp_tdi_retune/driver_type_probe.py), which offset a "
           "downshift's output-speed threshold by driver-type counter and brake "
           "pressure. Three blocks rather than two because BRAKE and DRIVING MODE "
           "are each the only cell that carries them, and both are load-bearing: "
           "the probes change the ZERO-BRAKE row only, so an event with the brake "
           "touched reads a different row and has to be thrown out, and driving "
           "mode is the only channel that might expose the driver-type counter -- "
           "there is no measuring block for the counter itself, and the dynamic "
           "shift program is NOT it (in program 10 its second value appears only "
           "when stationary at zero pedal). Losing the program number to the third "
           "block is the trade: stay in D for the whole log and do not touch the "
           "paddles, so the program is 10 throughout. Drive it in two halves. "
           "First, from a steady cruise in 5th at 55-75 km/h, squeeze to about a "
           "third pedal and HOLD -- a dozen times at different speeds, never "
           "touching the brake, which brackets the speed above which the downshift "
           "stops happening. Then drive briskly for a minute, several firm pedal "
           "applications, and repeat the same squeeze in 4th at 45-55 km/h: that "
           "half only shows anything if brisk driving actually moved the counter. "
           "Sample rate is ~5.6 Hz here, which is enough because the trigger is "
           "the PEDAL moving the threshold past a steady road speed -- the "
           "precision needed is in speed, not in time.",
           max_groups=3),
    Recipe("shifttrq", "Torque intervention during shifts (asked vs delivered)",
           ("tcu_trq", "tcu_nturb", "tcu_nout"),
           ("tcu_gear", "tcu_rpm", "tcu_pedal", "tcu_dsp"),
           "Fluid at 60 deg C or more. What matters is the few tenths of a second "
           "across each shift, so this is capped at TWO blocks to keep the rate up "
           "-- at 8 Hz a shift is three or four samples, at 4 Hz it is one and the "
           "intervention is invisible. Drive so that each event happens repeatedly "
           "and in isolation: a dozen part-throttle upshifts at 25, 35 and 50 % "
           "pedal, then a dozen closed-throttle coast-downs through every gear, then "
           "half a dozen kickdown downshifts from steady cruise in 4th and 5th. "
           "Leave a few seconds of steady driving between events -- the torque "
           "before the shift is the baseline the intervention is measured against, "
           "and without it the delivered change cannot be separated from whatever "
           "the driver was doing.",
           max_groups=2),
    Recipe("launch", "Pull-away gear (which gear it actually starts in)",
           ("tcu_gear", "tcu_nturb", "tcu_nout"),
           ("tcu_rpm", "tcu_pedal", "tcu_lever", "tcu_atf", "tcu_tcc"),
           "From cold, in D. Half a dozen standing starts at different pedal "
           "openings, including two gentle ones, with a full stop between each. "
           "Engaged gear and turbine speed are logged directly here, so this "
           "answers the launch-gear question outright -- engine rpm over road "
           "speed cannot, because converter slip is unbounded near a standstill.",
           max_groups=2),
    Recipe("program", "Shift program selection (which schedule is live)",
           ("tcu_prog", "tcu_dsp", "tcu_pedal"),
           ("tcu_lever", "tcu_gear", "tcu_vss", "tcu_atf"),
           "Start cold and log without stopping for at least fifteen minutes. "
           "Drive gently around town for the first five, then normally, then a "
           "few firm accelerations. This is what identifies which of the 82 "
           "schedules the controller is on and when it switches -- including "
           "whether a warm-up program is holding a gear.",
           max_groups=3),
    Recipe("tcc", "Converter lockup and slip",
           ("tcu_tcc", "tcu_slip", "tcu_pedal"),
           ("tcu_gear", "tcu_nturb", "tcu_rpm", "tcu_atf"),
           "Fluid at 60 deg C or more. In each of 3rd, 4th and 5th hold a steady "
           "speed just under where lockup engages, ease on until it locks, then "
           "back off until it releases. Then a few part-throttle accelerations so "
           "lockup and the shift schedule interact the way they do in traffic.",
           max_groups=3),
    Recipe("pressure", "Shift pressure and solenoid currents",
           ("tcu_pcv1", "tcu_pcv2", "tcu_gear"),
           ("tcu_pcv3", "tcu_pcv4", "tcu_pcv5", "tcu_pcv6", "tcu_atf"),
           "Fluid at 60 deg C or more. Moderate accelerations through every "
           "upshift, then kickdown downshifts from steady cruise. Watch the "
           "valve currents across the shift rather than between shifts.",
           max_groups=3),
)


@dataclass
class Resolution:
    """How a log's columns map onto roles."""

    columns: Dict[str, List[int]] = field(default_factory=dict)   # role -> column idx
    signals: List[str] = field(default_factory=list)              # per column

    def has(self, role: str) -> bool:
        return bool(self.columns.get(role))

    def missing(self, roles: Iterable[str]) -> List[str]:
        return [r for r in roles if not self.has(r)]


def role_of_signal(signal: str) -> Optional[str]:
    return _SIGNAL_ROLE.get(signal or "")


def _label_symbol(label: str) -> str:
    found = _SYMBOL_RE.findall(label or "")
    return found[-1] if found else ""


def resolve_log(log, namer=None) -> Resolution:
    """Identify every column of a :class:`~ovtsuite.datalog.logfile.DataLog`."""
    res = Resolution()
    for i, ch in enumerate(log.channels):
        sig = getattr(ch, "signal", "") or ""
        if not role_of_signal(sig) and namer is not None and getattr(ch, "group", 0) > 0:
            try:
                sig2 = namer.resolve(ch.group, ch.field)[1]
            except Exception:
                sig2 = ""
            if role_of_signal(sig2):
                sig = sig2
        if not role_of_signal(sig):
            sig3 = _label_symbol(getattr(ch, "label", ""))
            if role_of_signal(sig3):
                sig = sig3
        res.signals.append(sig)
        role = role_of_signal(sig)
        if role:
            res.columns.setdefault(role, []).append(i)
    return res


def _carries(namer) -> Dict[int, set]:
    """``{group: set of roles that group carries}``, populated groups only."""
    out: Dict[int, set] = {}
    for g in namer.populated_groups():
        got = set()
        for f in range(namer.slots(g)):
            r = role_of_signal(namer.resolve(g, f)[1])
            if r:
                got.add(r)
        if got:
            out[g] = got
    return out


def _cover(carries, roles, max_groups) -> Tuple[List[int], set]:
    """Greedy set cover over the blocks in ``carries``."""
    need = set(roles)
    chosen: List[int] = []
    while need and len(chosen) < max_groups:
        best = max(carries, key=lambda g: (len(carries[g] & need), -g), default=None)
        if best is None or not (carries[best] & need):
            break
        chosen.append(best)
        need -= carries.pop(best)
    return sorted(chosen), need


def plan_groups(namer, roles: Sequence[str], max_groups: int = 6,
                avoid: Sequence[int] = ()) -> Tuple[List[int], List[str]]:
    """Fewest populated measuring blocks that carry ``roles`` (greedy cover).

    Returns ``(groups, uncovered_roles)``. Groups are in ascending order, which
    is the order the logger reads them in.

    ``avoid`` lists blocks the ECU does not serve -- see ``datalog.served``.
    They are left out of the cover entirely, and a role that only lives in one
    of them is reported as uncovered. They used to come back as a "last
    resort", and that is how the 2026-10-10 cruise preset picked group 133 for
    the gear (a role it only *wanted*): the ECU answered N/A for the whole
    drive, as it has for every block of 100 or more ever logged on the car
    (133, 194, 213). An uncovered role is said up front; a dead column is only
    found after the drive.
    """
    if namer is None or not getattr(namer, "available", False):
        return [], list(roles)
    carries = _carries(namer)
    skip = set(int(g) for g in avoid)
    if skip:
        carries = {g: v for g, v in carries.items() if g not in skip}
    chosen, need = _cover(carries, roles, max_groups)
    return chosen, [r for r in roles if r in need]


def recipe_groups(namer, recipe: Recipe, max_groups: int = 6,
                  avoid: Sequence[int] = ()) -> Tuple[List[int], List[str]]:
    """Groups for a recipe: everything it needs first, then what it wants if
    that fits without dropping a needed role.

    ``avoid`` is passed through to both passes. It matters most on the wants
    pass: the needs are usually few enough to have a clean home, while the
    wants are what tempt the cover onto a block the ECU will not serve."""
    groups, missing = plan_groups(namer, list(recipe.needs), max_groups, avoid)
    if missing:
        return groups, missing
    extra, _ = plan_groups(namer, list(recipe.needs) + list(recipe.wants),
                           max_groups, avoid)
    # The wants-driven plan is a FRESH greedy cover, not an extension of the
    # needs-only one, so it can reshuffle onto blocks that happen to carry more
    # wanted roles and drop a needed one on the way. "Shift map discovery" is
    # where this first bit: needs turbine, which only group 1 carries, but
    # group 2 carries four of the wanted roles against group 1's three, so the
    # cover picked 2 + 16 and silently returned no turbine and no missing
    # roles. Fitting in max_groups was never the only condition -- the plan
    # also has to still cover everything the analysis cannot run without.
    if len(extra) <= max_groups and _covered(namer, extra).issuperset(recipe.needs):
        return extra, []
    return groups, []


def _covered(namer, groups: Sequence[int]) -> set:
    """Every role the given groups carry between them."""
    carries = _carries(namer)
    out: set = set()
    for g in groups:
        out |= carries.get(g, set())
    return out
