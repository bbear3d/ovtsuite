# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Name logged cells from the loaded binary.

A scan tool only knows "Group N / Field M"; the binary knows exactly what
each slot carries: EDC16 ``UACC_RSigBlkGrp_MAP`` cell -> ESB index -> the
signal catalogue (A2L measurement name, unit); EDC15 ``xcwKNN_x`` slot ->
``xcwGrpXX`` descriptor -> message name. So log columns can be labelled with
the ECU's own signal identities instead of label files.
"""
from typing import Dict, List, Optional, Tuple

from .logfile import Channel


class ChannelNamer:
    def __init__(self, session):
        self.session = session
        self._mb = None
        self._cat = None
        self._groups: Dict[int, object] = {}
        if session is None:
            return
        try:
            from ..msblock import MeasuringBlocks
            self._mb = MeasuringBlocks(session)
            if not self._mb.available:
                self._mb = None
        except Exception:
            self._mb = None
        if self._mb is None:
            return
        if getattr(session, "is_edc15", False):
            for g in self._mb.groups():
                self._groups.setdefault(g.block, g)      # first dataset's block N
        else:
            from ..signals import SignalCatalog
            self._cat = SignalCatalog(session.family, session)
            for g in self._mb.groups():
                self._groups[g.number] = g

    @property
    def available(self) -> bool:
        return bool(self._groups)

    def slots(self, group: int) -> int:
        g = self._groups.get(group)
        return g.slots if g is not None else 4

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        """(label, signal, unit) for a cell; generic label when unknown."""
        generic = ("Group %d / Cell %d" % (group, field + 1), "", "")
        g = self._groups.get(group)
        if g is None:
            return generic
        try:
            ids = g.channels()
        except Exception:
            return generic
        if field >= len(ids):
            return generic
        sid = ids[field]
        if getattr(self.session, "is_edc15", False):
            d = self._mb.descriptor(g.group, sid)
            if d is None or sid == 255:
                return generic
            name = d.name or ("xcwGrp%02d" % sid)
            return ("%s (G%d/%d)" % (name, group, field + 1), "xcwGrp%02d" % sid, "")
        if sid == 255 or self._cat is None:
            return generic
        info = self._cat.info(sid)
        if not info.known:
            return ("Group %d / Cell %d (signal %d)" % (group, field + 1, sid), "", "")
        human = info.human or info.name
        label = "%s (%s)" % (human, info.name) if info.human else info.name
        return (label, info.name, info.unit or "")

    def channel(self, group: int, field: int) -> Channel:
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def populated_groups(self) -> List[int]:
        return sorted(k for k, g in self._groups.items() if g.populated)

    def group_summary(self, group: int) -> str:
        g = self._groups.get(group)
        if g is None:
            return ""
        parts = []
        for f in range(g.slots):
            label, _sig, _u = self.resolve(group, f)
            if not label.startswith("Group "):
                parts.append(label.split(" (")[0])
        return ", ".join(parts)

    def find_signal(self, name: str) -> Optional[Tuple[int, int]]:
        """(group, field) of the first slot carrying an A2L signal name."""
        for grp in sorted(self._groups):
            for f in range(self.slots(grp)):
                if self.resolve(grp, f)[1] == name:
                    return grp, f
        return None
