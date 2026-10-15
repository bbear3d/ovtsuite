# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Task groups: the top level of the parameter tree.

A tuner opens a file to do something -- make power, fix drivability, sort out
emissions hardware, clear a fault -- so the tree is grouped by that, not by
the ECU supplier's software modules. Each group has a stable integer id, and
the id IS the sort order. ``Entity.tier`` carries the id (the field predates
this scheme; it used to hold a 1-4 "usefulness" tier).

Engine groups are 1-9, transmission groups 11-19, and the tail groups every
platform shares are 70+. Which group an entity lands in comes from, strongest
first:

1. the tuning catalogue baked into the knowledge base for that symbol
   (``tools/tuning_catalogue.py`` -> ``tools/bake_catalogue.py``);
2. the platform's module/function membership (``ovtsuite/tiers.py``,
   ``edc15/modules15.py``, ``zf6hp/tiers6hp.py``).
"""

from typing import Dict

# engine (EDC15 / EDC16)
POWER = 1
LIMITS = 2
DRIVE = 3
EMISSIONS = 6
OTHER_ENGINE = 7

#: Engine SYSTEMS -- not groups of their own. A module's parameters that the
#: catalogue does not name go to OTHER_ENGINE under a heading for their system
#: (LIMITS and DRIVE double as system ids for that purpose).
FUEL = 4
AIR = 5
SYSTEM_NAMES: Dict[int, str] = {
    FUEL: "Fuel & injection",
    AIR: "Boost & air",
    LIMITS: "Torque & limits",
    DRIVE: "Pedal, idle & cruise",
    EMISSIONS: "Emissions (EGR, DPF, swirl)",
}

# transmission (ZF 6HP)
SHIFT = 11
FEEL = 12
TCC = 13
GEARBOX_TORQUE = 14
MANUAL = 15
STRATEGY = 16          # a gearbox system, not a group of its own
ADAPTATION = 17
OTHER_GEARBOX = 18

# instrument cluster (VDO Mk4 EEPROM)
CLUSTER_GAUGES = 21
CLUSTER_CODING = 22
CLUSTER_IDENTITY = 23
CLUSTER_FIRMWARE = 24

#: parameters from a definition file packaged with an unrecognised binary
#: (an XDF in an .ovt project, see ovtsuite.xdf_session)
DEFINITION = 60
#: parameters the user defined themselves (ovtsuite.userparams)
USER = 61

# shared tail
DTC = 70
#: the second copies of coding-switched tables (``session.variant_entities``)
CODING = 78
VEHICLE = 80
SENSORS = 85
INTERNALS = 90
UNVERIFIED = 99

GROUP_NAMES: Dict[int, str] = {
    POWER: "Power",
    LIMITS: "Limiters & protection",
    DRIVE: "Drivability",
    EMISSIONS: "Emissions",
    OTHER_ENGINE: "Other engine maps",
    SHIFT: "Shift points",
    FEEL: "Shift feel",
    TCC: "Lockup (TCC)",
    GEARBOX_TORQUE: "Gearbox torque limits",
    MANUAL: "Manual mode (Tiptronic)",
    ADAPTATION: "Adaptation",
    OTHER_GEARBOX: "Other gearbox maps",
    CLUSTER_GAUGES: "Gauges & speedometer",
    CLUSTER_CODING: "Cluster coding & options",
    CLUSTER_IDENTITY: "Identity & immobilizer",
    CLUSTER_FIRMWARE: "Firmware patch module",
    DEFINITION: "From the definition file",
    USER: "User defined",
    DTC: "Fault codes (DTCs)",
    CODING: "Coding variants",
    VEHICLE: "Vehicle & swap config",
    SENSORS: "Sensors & models",
    INTERNALS: "Internals / OBD",
    UNVERIFIED: "Unverified placement",
}

#: One line per group, for the tree's hover text.
GROUP_HELP: Dict[int, str] = {
    POWER: "The maps a power tune is made of: driver's wish, torque and "
           "smoke limiters, torque-to-fuel, boost target and limit, "
           "injection timing and duration.",
    LIMITS: "Rev, thermal, turbo and gearbox torque protection, and the "
            "limp-mode limits.",
    DRIVE: "Idle speeds and pedal feel.",
    OTHER_ENGINE: "Supporting maps: everything in the fuel, boost, torque "
                  "and pedal/idle modules that a tune does not normally "
                  "touch -- corrections, controller gains, models.",
    EMISSIONS: "The EGR, DPF and swirl-flap settings tuners change: EGR "
               "target and shut-off, EGR valve duty, regeneration switch "
               "and interval, swirl flaps. The rest of those modules is in "
               "Other engine maps (Advanced).",
    SHIFT: "When the gearbox shifts: shift schedules per driving mode, and "
           "the mode-selection tables.",
    FEEL: "How the gearbox shifts: clutch pressures, fill, slip phase and "
          "shift timing.",
    TCC: "Torque converter lockup schedules and lockup-clutch control.",
    GEARBOX_TORQUE: "How much torque the gearbox accepts, and the engine "
                    "torque cut it requests while shifting.",
    MANUAL: "Tiptronic / manual-mode behaviour and its speed limits.",
    OTHER_GEARBOX: "Supporting gearbox data: shift-sequence timing and "
                   "controller terms, lockup control internals, driving "
                   "strategy (driver type, hill, braking, cornering).",
    ADAPTATION: "Learned-value adaptation: fill pressure, shift pressure, "
                "flare and gear engagement.",
    CLUSTER_GAUGES: "K-value, speedometer, tachometer, fuel and temperature "
                    "needle maps, fuel consumption trim.",
    CLUSTER_CODING: "Soft-coding digits, vehicle family, signal sources, "
                    "lighting, ice warning and trip-computer options.",
    CLUSTER_IDENTITY: "VIN, part number, immobilizer data, SKC, odometer and "
                      "radio ID -- what a cluster replacement carries over.",
    CLUSTER_FIRMWARE: "The memory-patch block the cluster loads into RAM at "
                      "boot (bug fixes, needle sweep). Code for one exact ROM "
                      "build.",
    DEFINITION: "Every table and value of the definition file packaged with "
                "this binary in its project (an XDF), grouped by its own "
                "categories.",
    USER: "Parameters you defined yourself (right-click > Add parameter...), "
          "kept with the project.",
    DTC: "Fault-path enables, classes and debounce -- what sets a code "
         "and what the ECU does about it.",
    CODING: "The second copy of every table the ECU switches by coding "
            "(e.g. automatic vs manual), labelled with the coding(s) that "
            "select it and filed under the group its main copy is in. The "
            "main copies stay where they are. Copies of supporting maps are "
            "behind Advanced, like their main copies.",
    VEHICLE: "Coding, variant switches, CAN, A/C, glow, fans, immobilizer "
             "-- what changes for a swap or a different car.",
    SENSORS: "Sensor linearisation, signal conditioning, models and "
             "monitoring functions.",
    INTERNALS: "OBD frame data, network management, signal tests and other "
               "plumbing.",
    UNVERIFIED: "Parameters whose address could not be confirmed on this "
                "file. Check before editing.",
}


#: Groups hidden until "Advanced" is ticked: supporting and plumbing
#: parameters a tune does not normally touch.
ADVANCED = frozenset({OTHER_ENGINE, OTHER_GEARBOX, SENSORS, INTERNALS,
                      ADAPTATION, UNVERIFIED, CLUSTER_FIRMWARE})

#: Gearbox systems whose un-headline data goes to OTHER_GEARBOX.
GEARBOX_SYSTEM_NAMES: Dict[int, str] = {
    SHIFT: "Shift points",
    FEEL: "Shift feel",
    TCC: "Lockup (TCC)",
    GEARBOX_TORQUE: "Gearbox torque limits",
    MANUAL: "Manual mode (Tiptronic)",
    STRATEGY: "Driving strategy",
}


def group_name(gid: int) -> str:
    return GROUP_NAMES.get(gid, "Other")


def is_advanced(e) -> bool:
    """Whether ``e`` is behind the "Advanced" toggle: its group is, or -- for
    a copy in Coding variants -- its main copy's group is. Q and R carry
    about 700 copies each, most of them of supporting maps."""
    t = getattr(e, "tier", None)
    if t == CODING:
        return getattr(e, "base_tier", 0) in ADVANCED
    return t in ADVANCED
