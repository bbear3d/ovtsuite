# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The garage: the vehicles this copy of OVT Diag knows and the scans saved
with each, as plain JSON beside the app's settings::

    <config>/diag/garage/<vehicle-id>/vehicle.json
    <config>/diag/garage/<vehicle-id>/scans/<yyyymmdd-hhmmss>.json

Files, not QSettings, for the same reason the head-unit pairings are
(``flash/headunits.py``): typed data survives, and a scan is something the
user may want to copy, attach or diff with another tool.
"""
import json
import os
import re
import time
from typing import List, Optional

from .model import ScanResult, Vehicle


def root() -> str:
    from ..flash.headunits import config_dir
    return os.path.join(config_dir(), "diag", "garage")


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s or "vehicle"


class Garage:
    def __init__(self, path: Optional[str] = None):
        self.path = path or root()

    # --------------------------------------------------------------- vehicles
    def vehicles(self) -> List[Vehicle]:
        out = []
        if not os.path.isdir(self.path):
            return out
        for name in sorted(os.listdir(self.path)):
            v = self.vehicle(name)
            if v is not None:
                out.append(v)
        out.sort(key=lambda v: v.created, reverse=True)
        return out

    def vehicle(self, vid: str) -> Optional[Vehicle]:
        p = os.path.join(self.path, vid, "vehicle.json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return Vehicle.from_dict(json.load(fh))
        except (OSError, ValueError):
            return None

    def new_vehicle(self, name: str, vin: str = "", generation: str = "", notes: str = "") -> Vehicle:
        base = slug(vin or name)
        vid, n = base, 1
        while os.path.exists(os.path.join(self.path, vid)):
            n += 1
            vid = "%s-%d" % (base, n)
        v = Vehicle(id=vid, name=name, vin=vin, generation=generation, notes=notes)
        self.save_vehicle(v)
        return v

    def save_vehicle(self, v: Vehicle) -> None:
        d = os.path.join(self.path, v.id)
        os.makedirs(os.path.join(d, "scans"), exist_ok=True)
        _write_json(os.path.join(d, "vehicle.json"), v.to_dict())

    def delete_vehicle(self, vid: str) -> None:
        import shutil
        d = os.path.join(self.path, vid)
        if os.path.isdir(d):
            shutil.rmtree(d)

    # ------------------------------------------------------------------ scans
    def scans(self, vid: str) -> List[str]:
        """Scan file names for a vehicle, newest first."""
        d = os.path.join(self.path, vid, "scans")
        if not os.path.isdir(d):
            return []
        return sorted((f for f in os.listdir(d) if f.endswith(".json")), reverse=True)

    def scan_path(self, vid: str, name: str) -> str:
        return os.path.join(self.path, vid, "scans", name)

    def load_scan(self, vid: str, name: str) -> Optional[ScanResult]:
        p = self.scan_path(vid, name)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return ScanResult.from_dict(json.load(fh))
        except (OSError, ValueError):
            return None

    def save_scan(self, scan: ScanResult) -> str:
        d = os.path.join(self.path, scan.vehicle_id, "scans")
        os.makedirs(d, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(scan.when)) + ".json"
        p = os.path.join(d, name)
        n = 1
        while os.path.exists(p):
            n += 1
            p = os.path.join(d, name[:-5] + "-%d.json" % n)
        _write_json(p, scan.to_dict())
        return p

    def latest_scan(self, vid: str) -> Optional[ScanResult]:
        names = self.scans(vid)
        return self.load_scan(vid, names[0]) if names else None


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def scan_label(name: str) -> str:
    """``20261025-211400.json`` -> ``2026-10-25 21:14``."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
    if not m:
        return name
    y, mo, d, h, mi, s = m.groups()
    return "%s-%s-%s %s:%s" % (y, mo, d, h, mi)
