# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 task-group membership for parameters the tuning catalogue does not
name, by Bosch module prefix.

The module (the A2L name up to the first underscore) is the supplier's own
statement of which software function a parameter belongs to, so it decides
the group. The catalogue baked into the knowledge base (``tools/
tuning_catalogue.py``) overrides this for the maps it names -- that is how the
handful of headline maps out of a module land in Power while the rest of the
module stays with its system.

Historically ``Entity.tier`` held a 1-4 usefulness tier; it now holds the task
group id from ``ovtsuite.groups`` (which is also the sort order). The names
``tier_of`` / ``tier_name`` / ``TIER_NAMES`` are kept so callers need not
change.

Two Bosch naming tokens are structural rather than modular and are applied
before the prefix: ``Rgn`` (regeneration) and ``EGT`` (exhaust-gas
treatment -- NOT temperature on these ECUs; see ``EGT_swtEGTActv_C``, "switch
value to turn EGT functions on or off", in the regeneration coordinator).
A boost, fuel or timing map with either token in its name is the DPF-
regeneration version of that map, so it belongs with Emissions.
"""

import re

from . import groups as G
from .groups import GROUP_NAMES as TIER_NAMES, group_name as tier_name  # noqa: F401

_PREFIX_GROUPS = {
    # ---- limiters / protection
    "EngPrt": G.LIMITS, "TrqLim": G.LIMITS, "CoEng": G.LIMITS,
    "CoVeh": G.LIMITS, "CoVM": G.LIMITS, "CoDT": G.LIMITS, "DrvTrn": G.LIMITS,
    # ---- drivability
    "AccPed": G.DRIVE, "Prp": G.DRIVE, "PrpCtl": G.DRIVE, "LIGov": G.DRIVE,
    "CrCtl": G.DRIVE, "CrCCD": G.DRIVE, "ACC": G.DRIVE, "ACCCD": G.DRIVE,
    "ASDdc": G.DRIVE, "ASDrf": G.DRIVE, "StSys": G.DRIVE,
    "StSysCtl": G.DRIVE, "StrtCD": G.DRIVE, "T50CD": G.DRIVE,
    # ---- fuel & injection
    "FMTC": G.FUEL, "FBC": G.FUEL, "FlMng": G.FUEL, "InjCrv": G.FUEL,
    "InjVlv": G.FUEL, "InjCtl": G.FUEL, "InjVCD": G.FUEL, "InjUn": G.FUEL,
    "BIP": G.FUEL, "BIPCD": G.FUEL, "FlSys": G.FUEL, "FMA": G.FUEL,
    "Smk": G.FUEL, "FlPpCD": G.FUEL, "PSP": G.FUEL, "PSPCD": G.FUEL,
    "CmbChb": G.FUEL,
    # ---- boost & air
    "PCR": G.AIR, "BstCtl": G.AIR, "ThrVlv": G.AIR, "IndSys": G.AIR,
    "AFVCtl": G.AIR, "AFVCD": G.AIR, "TVACD": G.AIR, "TVASCD": G.AIR,
    "BPACD": G.AIR, "BPASCD": G.AIR, "AirSys": G.AIR,
    # ---- emissions
    "AirCtl": G.EMISSIONS, "EGR": G.EMISSIONS, "EGRVlv": G.EMISSIONS,
    "EGRCD": G.EMISSIONS, "EGRSCD": G.EMISSIONS, "PFlt": G.EMISSIONS,
    "PFltCD": G.EMISSIONS, "EGSys": G.EMISSIONS, "EGT": G.EMISSIONS,
    "EGPp": G.EMISSIONS, "EGPpCD": G.EMISSIONS, "OxiCCD": G.EMISSIONS,
    "AddPCD": G.EMISSIONS,
    # ---- fault codes
    "DSM": G.DTC, "DSMDur": G.DTC, "Dfp": G.DTC,
    # ---- vehicle & swap config
    "GlwCtl": G.VEHICLE, "GlwCD": G.VEHICLE, "FanCtl": G.VEHICLE,
    "FanCD": G.VEHICLE, "CtT": G.VEHICLE, "CtTCtl": G.VEHICLE,
    "CtTCD": G.VEHICLE, "Clg": G.VEHICLE, "AOHt": G.VEHICLE,
    "AOHtCD": G.VEHICLE, "ACCtl": G.VEHICLE, "ACCD": G.VEHICLE,
    "ACTrqC": G.VEHICLE, "AC": G.VEHICLE, "Gearbx": G.VEHICLE,
    "VSSCD": G.VEHICLE, "VehDa": G.VEHICLE, "ImmCtl": G.VEHICLE,
    "Immo": G.VEHICLE, "UKey": G.VEHICLE, "UAcc": G.VEHICLE,
    "Uacc": G.VEHICLE, "UACC": G.VEHICLE, "CANTx": G.VEHICLE,
    "Alt": G.VEHICLE, "AltCD": G.VEHICLE, "SrvPmp": G.VEHICLE,
    "FlClgCD": G.VEHICLE, "ConvCD": G.VEHICLE, "Conv": G.VEHICLE,
    "BattCD": G.VEHICLE, "Batt": G.VEHICLE, "AirbCD": G.VEHICLE,
    "MRlyCD": G.VEHICLE, "ErLp": G.VEHICLE, "ErLpCD": G.VEHICLE,
    "SvLp": G.VEHICLE, "SvLpCD": G.VEHICLE, "EEP": G.VEHICLE,
    "EEPCD": G.VEHICLE, "EepInit": G.VEHICLE, "EepDev": G.VEHICLE,
    "Tp20": G.VEHICLE, "Diff": G.VEHICLE, "ESS": G.VEHICLE,
    # ---- sensors & models (sensor drivers, models, monitoring)
    "ASMod": G.SENSORS, "EngM": G.SENSORS, "Eng": G.SENSORS,
    "LSU": G.SENSORS, "LSUCD": G.SENSORS, "LSUCtl": G.SENSORS,
    "AFSCD": G.SENSORS, "APPCD": G.SENSORS, "APSCD": G.SENSORS,
    "CaSCD": G.SENSORS, "CrSCD": G.SENSORS, "CrS": G.SENSORS,
    "OTSCD": G.SENSORS, "CTSCD": G.SENSORS, "COTSCD": G.SENSORS,
    "BPSCD": G.SENSORS, "IATSCD": G.SENSORS, "EATSCD": G.SENSORS,
    "FTSCD": G.SENSORS, "FQTSCD": G.SENSORS, "FQlSCD": G.SENSORS,
    "FlSCD": G.SENSORS, "HDSCD": G.SENSORS, "LASCD": G.SENSORS,
    "GbSSCD": G.SENSORS, "WDASCD": G.SENSORS, "WDALCD": G.SENSORS,
    "BrkCD": G.SENSORS, "T15CD": G.SENSORS, "VSACD": G.SENSORS,
    "EMCCD": G.SENSORS, "CtApCD": G.SENSORS, "IFCD": G.SENSORS,
    "IFCtl": G.SENSORS, "MSidCD": G.SENSORS, "MSid": G.SENSORS,
    "USAp": G.SENSORS, "SSpMon": G.SENSORS, "OvRMon": G.SENSORS,
    "Shtrp": G.SENSORS, "TAA": G.SENSORS, "BasAdj": G.SENSORS,
    "MoC": G.SENSORS, "MoF": G.SENSORS, "MoFPrp": G.SENSORS,
    "EngMon": G.SENSORS, "ADCMon": G.SENSORS, "HWEMon": G.SENSORS,
    "TPUMon": G.SENSORS, "MemMon": G.SENSORS, "ARnMon": G.SENSORS,
    "SPMon": G.SENSORS, "ExeMon": G.SENSORS,
    # ---- internals / OBD
    "FrmMng": G.INTERNALS, "Sig": G.INTERNALS, "SigTst": G.INTERNALS,
    "Signals": G.INTERNALS, "signals": G.INTERNALS, "NetMng": G.INTERNALS,
    "KWP": G.INTERNALS, "OBD": G.INTERNALS, "Diag": G.INTERNALS,
    "DIUMPR": G.INTERNALS, "Carb": G.INTERNALS, "CARB": G.INTERNALS,
    "TstTog": G.INTERNALS, "Tst": G.INTERNALS, "SOPTst": G.INTERNALS,
    "Rtmo": G.INTERNALS, "BypEtk": G.INTERNALS, "BypInt": G.INTERNALS,
    "UAddr": G.INTERNALS, "McMess": G.INTERNALS, "LockCnt": G.INTERNALS,
    "K2rffd": G.INTERNALS, "EcuInit": G.INTERNALS, "ECU": G.INTERNALS,
    "DSCHED": G.INTERNALS, "Chiste": G.INTERNALS, "NSC": G.INTERNALS,
    "CcpAcquisitionConfig": G.INTERNALS,
}

#: Anything with no known module: the honest place is with the plumbing,
#: but visibly, never mixed into a tuning group.
DEFAULT_GROUP = G.INTERNALS

_REGEN = re.compile(r"(Rgn|EGT)(?=[A-Z0-9_]|$)")

#: Systems whose un-named parameters are supporting maps.
_SUPPORTING = frozenset(G.SYSTEM_NAMES)


def system_of(name: str) -> int:
    """The module's system (or task group, for modules that are one)."""
    prefix = name.split("_", 1)[0]
    rest = name[len(prefix):]
    if prefix == "DSM" or prefix.startswith("DSM"):
        return G.DTC
    if _REGEN.search(rest) and prefix in ("PCR", "AirCtl", "FMTC", "InjCrv",
                                          "InjVlv", "FlMng", "ThrVlv",
                                          "LIGov", "InjCtl"):
        return G.EMISSIONS
    g = _PREFIX_GROUPS.get(prefix)
    if g is None:
        # longest known module the prefix starts with (e.g. 'LSUCtl...')
        best = 0
        for k, v in _PREFIX_GROUPS.items():
            if prefix.startswith(k) and len(k) > best:
                g, best = v, len(k)
    return g if g is not None else DEFAULT_GROUP


def tier_of(name: str, description: str = "") -> int:
    """Task group for an EDC16 symbol the catalogue does not name.

    Fuel, boost, torque/limit and pedal/idle modules are where the headline
    maps come from; what the catalogue does not lift out of them is
    supporting material, filed under "Other engine maps"."""
    g = system_of(name)
    return G.OTHER_ENGINE if g in _SUPPORTING else g


def system_label(name: str) -> str:
    """Heading for an "Other engine maps" parameter: its system."""
    return G.SYSTEM_NAMES.get(system_of(name), "Other")


#: Vehicle & swap config, by system: module -> heading.
VEHICLE_SYSTEMS = (
    ("Cooling & thermostat", ("CtT", "CtTCtl", "CtTCD", "Clg", "FanCtl",
                              "FanCD", "FlClgCD")),
    ("Glow plugs", ("GlwCtl", "GlwCD")),
    ("A/C & heater", ("ACCtl", "ACCD", "ACTrqC", "AC", "AOHt", "AOHtCD")),
    ("Gearbox & road speed", ("Gearbx", "VSSCD", "VehDa", "Diff", "ConvCD",
                              "Conv")),
    ("CAN & communication", ("CANTx", "Tp20", "ESS")),
    ("Immobilizer & coding", ("ImmCtl", "Immo", "UKey", "UAcc", "Uacc",
                              "UACC", "EEP", "EEPCD", "EepInit", "EepDev")),
    ("Electrical", ("Alt", "AltCD", "BattCD", "Batt", "MRlyCD", "ErLp",
                    "ErLpCD", "SvLp", "SvLpCD", "AirbCD", "SrvPmp")),
)
_VEHICLE_SYSTEM = {m: name for name, mods in VEHICLE_SYSTEMS for m in mods}

#: The seven settings every Bosch fault path has, by the DSM_<kind>Dfp_
#: symbol they live in (meanings from the A2L descriptions).
DTC_KINDS = {
    "ClaDfp": "Fault class",                 # "Error class for error path"
    "FMKDfp": "Hiding conditions",           # "Hiding conditions for ..."
    "InhDfp": "Functions it locks",          # "Function identifiers locked by"
    "EnvDfp": "Freeze-frame data",           # "...environmental conditions"
    "TypDfp": "Fault type mapping",          # "Conversion of error types ..."
    "CDKDfp": "VAG fault code",              # "Customer-specific error code"
    "CDCDfp": "OBD code",                    # "CARB specific error code"
}
_DSM_PATH = re.compile(r"^DSM_([A-Za-z]+Dfp)_(.+?)(_C)?$")

#: Fault-path headings the shared module-title table lacks or words poorly.
_DTC_TITLES = {
    "FanCD": "Cooling Fan", "GlwCD": "Glow Plug", "VSSCD": "Vehicle Speed",
    "StrtCtl": "Starter", "StrtCD": "Starter", "StSysCtl": "Starter",
    "StSys": "Starter", "T50CD": "Starter", "T15CD": "Ignition (T15)",
    "BPACD": "Boost Actuator", "TVACD": "Throttle", "TVASCD": "Throttle",
    "ThrVlv": "Throttle", "EMCCD": "Engine Mounts", "ConvCD": "Converter",
    "VSACD": "Swirl Flaps", "GbSSCD": "Gearbox", "WDALCD": "Water Pump",
    "WdCom": "Watchdog", "SysLamp": "Warning Lamps", "PrpCtl": "Speed Control",
    "SrvPmp": "Steering Pump", "PSPCD": "Fuel Pump", "SSpMon": "Sensor Supply",
    "OTSCD": "Oil", "APPCD": "Accelerator Pedal", "AFSCD": "Air Mass",
    "Shtrp": "Short Trip Tests", "USAp": "Coding", "UAcc": "Coding",
    "TPUMon": "HW Monitor", "SOPTst": "HW Monitor",
    "FrmMng": "CAN messages", "CANA": "CAN bus", "CANB": "CAN bus",
    "CANC": "CAN bus", "CANBus": "CAN bus", "CANErr": "CAN bus",
    "APP": "Accelerator Pedal", "MIL": "Warning Lamps", "Montr": "HW Monitor",
    "ACC": "Adaptive Cruise", "ACCCD": "Adaptive Cruise", "ADCMon": "HW Monitor", "EngMon": "HW Monitor",
    "OvRMon": "Overrun Monitoring", "BrkCD": "Brake Switch",
    "CtApCD": "Coolant", "FQTSCD": "Fuel", "FQlSCD": "Fuel", "FlSCD": "Fuel",
    "HDSCD": "Hall Sensor", "IFCD": "Fuel Pump", "LASCD": "Lambda",
    "LSUCtl": "Lambda", "AFVCD": "Air Filter Valve", "IndSys": "Swirl Flaps",
    "AirbCD": "Airbag", "Clg": "Cooling", "CtT": "Coolant", "CtTCD": "Coolant",
    "Diff": "Differential", "ErLpCD": "Warning Lamps", "FlClgCD": "Fuel Cooling",
    "MRlyCD": "Main Relay", "BIP": "Injector", "CmbChb": "Combustion",
    "FlPpCD": "Fuel Pump", "CoVM": "Vehicle Coordinator",
    "CoVeh": "Vehicle Coordinator",
}


def _module_of(token: str) -> str:
    """Longest known module name a fault-path token starts with
    ("PFltTempUpStrm" -> "PFlt", "VSSCDNpl" -> "VSSCD")."""
    best = ""
    for k in _PREFIX_GROUPS:
        if token.startswith(k) and len(k) > len(best):
            best = k
    return best


def dtc_place(name: str):
    """(path, plain) for a fault-code parameter: a fault path's seven
    settings sit together under its system -- "DPF > PFltTempUpStrm >
    Fault class" -- and the fault manager's own settings under one heading.
    """
    m = _DSM_PATH.match(name)
    if not m or m.group(1) not in DTC_KINDS:
        return ["Fault manager settings"], ""
    path = m.group(2)
    mod = _module_of(path)
    if not mod:
        mod = next((k for k in sorted(_DTC_TITLES, key=len, reverse=True)
                    if path.startswith(k)), "")
    title = _DTC_TITLES.get(mod, "")
    if not title:
        try:
            from .signal_names import _MODULE
            title = _MODULE.get(mod, "")
        except Exception:
            title = ""
    if not title and mod:
        g = system_of(mod + "_x")
        title = G.SYSTEM_NAMES.get(g) or G.GROUP_NAMES.get(g, "")
    return [title or "Other", path], DTC_KINDS[m.group(1)]


def place(e, cat=None) -> None:
    """Give an EDC16 entity its group and tree path: the baked catalogue
    record when it has one, otherwise its module's group -- and a supporting
    map goes under its system, then its A2L function. The one place this is
    decided, shared by the session and the search-index builder."""
    from . import catalogue as _catalogue
    e.tier = tier_of(e.name, e.description)
    _catalogue.apply(e, cat)
    if e.plain:
        return
    heading = _catalogue.heading_text(
        e.categories[0] if e.categories else "", e.name)
    if e.tier == G.OTHER_ENGINE:
        e.category_path = [system_label(e.name), heading]
    elif e.tier == G.VEHICLE:
        sysname = _VEHICLE_SYSTEM.get(e.name.split("_", 1)[0], "Other")
        e.category_path = [sysname, heading]
    elif e.tier == G.DTC:
        path, plain = dtc_place(e.name)
        e.category_path = path
        if plain:
            e.plain = plain        # "Fault class", under its fault path
