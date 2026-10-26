# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fitted hardware: what is on the engine a set's calibration is for.

A record says "from <date> the car has <hardware>, calibrated for in the
versions after <checkpoint>". It answers two different questions:

* **which hardware a version of the file was calibrated for**
  (:func:`for_checkpoint`) -- the dyno chart and Tools > Injector setup use
  it, so two versions set up for different nozzles are not compared as
  though the engine were the same;
* **which hardware was on the car at a time** (:func:`at`) -- logs are
  sorted into "before" and "after" a change with it.

Records live in the set, ``BinarySet.extra["hardware"]`` (a list, oldest
first), so a project written by a version that does not know them keeps
them. One record per change; ``kind`` says which part changed (several kinds
can be current at once), and ``parts`` is the tool's own description of it,
which this module never interprets:

====================  =======================================================
``ve_pump``           EDC15 VE (VP37) injection pump and nozzles: ``plunger_mm``,
                      ``nozzle`` (Tools > Injector setup on a VE file)
``pd_injectors``      unit injectors (PD): ``preset``, ``body`` (Tools >
                      Injector setup on a PD file)
====================  =======================================================

``checkpoint`` is the version the setup was made on top of. With ``pending``
the calibration itself is in the edits not yet saved then, so it belongs to
the versions *after* that checkpoint; without it (a record made on a saved
version, by hand), to that version too. A record without a checkpoint says
only what the car has, not which calibration is for it.

Base app, no Qt, no module imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import new_id, now

KEY = "hardware"

VE_PUMP = "ve_pump"
PD_INJECTORS = "pd_injectors"
KIND_LABELS = {
    VE_PUMP: "Injection pump and nozzles",
    PD_INJECTORS: "Unit injectors",
}

#: where a record came from
FROM_SETUP = "injector-setup"
FROM_USER = "user"
FROM_AI = "ai"


@dataclass
class Fitted:
    id: str
    kind: str
    summary: str                                  # one line, the tool's own words
    parts: Dict[str, Any] = field(default_factory=dict)
    since: str = ""                               # when the car got it (ISO date/time)
    checkpoint: Optional[str] = None              # the calibration's version (see module doc)
    pending: bool = False
    branch: Optional[str] = None
    source: str = FROM_USER
    note: str = ""
    recorded: str = ""                            # when the record was made
    extra: Dict[str, Any] = field(default_factory=dict)

    _FIELDS = ("id", "kind", "summary", "parts", "since", "checkpoint", "pending",
               "branch", "source", "note", "recorded")

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.extra)
        for k in self._FIELDS:
            out[k] = getattr(self, k)
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fitted":
        if not isinstance(d, dict) or not d.get("id"):
            raise ValueError("a hardware record needs an id")
        kw = {k: d[k] for k in cls._FIELDS if k in d}
        kw.setdefault("kind", "")
        kw.setdefault("summary", "")
        extra = {k: v for k, v in d.items() if k not in cls._FIELDS}
        return cls(extra=extra, **kw)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind.replace("_", " ").capitalize())

    def same_parts(self, other: Optional["Fitted"]) -> bool:
        return other is not None and other.kind == self.kind and other.parts == self.parts


# ------------------------------------------------------------------ reading
def _raw(project, sid: str) -> list:
    v = project.set(sid).extra.get(KEY)
    return v if isinstance(v, list) else []


def records(project, sid: str, kind: Optional[str] = None) -> List[Fitted]:
    """The set's records, oldest first (by ``since``, then when recorded).
    A record this version cannot read is left out (and kept in the file)."""
    out = []
    for d in _raw(project, sid):
        try:
            r = Fitted.from_dict(d)
        except (ValueError, TypeError):
            continue
        if kind is None or r.kind == kind:
            out.append(r)
    out.sort(key=lambda r: (r.since or r.recorded or "", r.recorded or ""))
    return out


def get(project, sid: str, rid: str) -> Optional[Fitted]:
    return next((r for r in records(project, sid) if r.id == rid), None)


def current(project, sid: str, kind: Optional[str] = None) -> List[Fitted]:
    """What the car has now: the newest record of each kind."""
    last: Dict[str, Fitted] = {}
    for r in records(project, sid, kind):
        last[r.kind] = r
    return list(last.values())


def at(project, sid: str, when: str, kind: Optional[str] = None) -> List[Fitted]:
    """What the car had at ``when`` (ISO text, as the project's timestamps):
    the newest record of each kind fitted by then. Empty = what it came
    with (nothing recorded)."""
    last: Dict[str, Fitted] = {}
    for r in records(project, sid, kind):
        # ISO text compares in time order; a date alone ("2026-10-24") is
        # the start of that day
        if r.since and when and r.since <= when:
            last[r.kind] = r
    return list(last.values())


def for_checkpoint(project, sid: str, cid: Optional[str], kind: Optional[str] = None,
                   *, unsaved: bool = False) -> List[Fitted]:
    """The hardware version ``cid`` is calibrated for, one record per kind
    (none = calibrated for what the car came with, or nothing recorded).
    ``unsaved``: the state is ``cid`` plus edits not saved yet -- a record
    pending on ``cid`` itself then counts too."""
    s = project.set(sid)
    chain = [c.id for c in s.lineage(cid)] if cid else []
    pos = {c: i for i, c in enumerate(chain)}
    best: Dict[str, tuple] = {}
    for r in records(project, sid, kind):
        if not r.checkpoint or r.checkpoint not in pos:
            continue
        i = pos[r.checkpoint]
        if r.pending and i == 0 and not unsaved:
            continue        # the setup's edits were not saved into this version
        rank = (-i, r.recorded or "")
        if r.kind not in best or rank > best[r.kind][0]:
            best[r.kind] = (rank, r)
    return [v[1] for v in best.values()]


def describe(rec: Fitted, project=None, sid: str = "") -> str:
    """"11 mm pump head, P1019 nozzles -- fitted 2026-10-24, set up after
    version 'Stage 1'"."""
    bits = []
    if rec.since:
        bits.append("fitted %s" % rec.since.replace("T", " ")[:16])
    if rec.checkpoint and project is not None and sid:
        c = project.set(sid).checkpoint(rec.checkpoint)
        if c is not None:
            name = c.label or c.kind
            bits.append(("set up after version '%s'" if rec.pending
                         else "calibrated in version '%s'") % name)
    return rec.summary + (" -- " + ", ".join(bits) if bits else "")


def _what(r: Fitted) -> dict:
    """What a record says is fitted: its parts, or (a record entered by
    hand, with none) its words."""
    return r.parts or {"summary": r.summary.strip().lower()}


def differ(a: List[Fitted], b: List[Fitted]) -> List[str]:
    """The kinds whose hardware is not the same in ``a`` and ``b``."""
    ka = {r.kind: r for r in a}
    kb = {r.kind: r for r in b}
    out = []
    for k in sorted(set(ka) | set(kb)):
        x, y = ka.get(k), kb.get(k)
        if x is None or y is None or _what(x) != _what(y):
            out.append(k)
    return out


def split_logs(project, sid: str, kind: str):
    """``(record, after, before)``: the newest record of ``kind`` and the
    project's logs added since it was fitted / before (newest first), for
    "the logs since the change". ``(None, [], [])`` when nothing with a date
    is recorded."""
    cur = current(project, sid, kind)
    if not cur or not cur[0].since:
        return None, [], []
    when = cur[0].since
    logs = sorted((a for a in project.logs if a.added), key=lambda a: a.added, reverse=True)
    return (cur[0], [a for a in logs if a.added >= when],
            [a for a in logs if a.added < when])


def report(project, sid: str, bid: str, kind: str, *, unsaved: bool = False) -> dict:
    """What the project records for ``kind``, as plain data (the AI tools):
    the hardware fitted now, and what branch ``bid``'s head (with
    ``unsaved`` edits) is set up for."""
    def row(r):
        return {"summary": r.summary, "parts": r.parts or None, "fitted": r.since or None,
                "describe": describe(r, project, sid), "source": r.source,
                "note": r.note or None}
    cur = current(project, sid, kind)
    b = project.set(sid).branch(bid) if bid else project.set(sid).active
    cal = for_checkpoint(project, sid, b.head if b is not None else None, kind,
                         unsaved=unsaved)
    out = {"recorded": row(cur[0]) if cur else None,
           "this_version_set_up_for": row(cal[0]) if cal else None}
    if cur and not cal:
        out["note"] = "this version is not set up for the recorded hardware yet"
    elif cur and cal and not cur[0].same_parts(cal[0]) and cur[0].parts:
        out["note"] = "this version is set up for other hardware than the car has now"
    return out


def setup_changes(project, sid: str, cid: Optional[str], *,
                  unsaved: bool = False) -> List[Fitted]:
    """The hardware setup that starts at version ``cid``: the records it is
    set up for whose hardware differs from what its previous version was
    set up for (``unsaved``: ``cid`` plus edits not saved yet, against
    ``cid`` itself). Empty when the setup does not change there -- the
    version history and the Project panel mark the versions that are not."""
    if not cid or not _raw(project, sid):     # nothing recorded (most sets): cheap
        return []
    now_ = for_checkpoint(project, sid, cid, unsaved=unsaved)
    if unsaved:
        before = for_checkpoint(project, sid, cid)
    else:
        c = project.set(sid).checkpoint(cid)
        before = for_checkpoint(project, sid, c.parent) if c is not None and c.parent else []
    kinds = set(differ(now_, before))
    return [r for r in now_ if r.kind in kinds]


def summaries(recs: List[Fitted]) -> str:
    """"PD150 injectors; 11 mm pump head" -- the records' own words."""
    return "; ".join(r.summary for r in recs)


def status(project, sid: str, cid: Optional[str], *, unsaved: bool = False) -> dict:
    """What is fitted now against what version ``cid`` (``unsaved``: with
    edits not saved yet) is set up for, as plain data: ``fitted`` and
    ``set_up_for`` (record lists) and ``differ`` (the kinds that are not the
    same). ``fitted`` empty = nothing recorded."""
    cur = current(project, sid)
    cal = for_checkpoint(project, sid, cid, unsaved=unsaved) if cid else []
    return {"fitted": cur, "set_up_for": cal, "differ": differ(cur, cal) if cur else []}


def status_text(project, sid: str, cid: Optional[str], *,
                unsaved: bool = False) -> tuple:
    """``(line, details)`` for the Project panel, a tab's tooltip and the
    flash write: "Fitted: PD150 injectors" when version ``cid`` is set up
    for what the car has, else "Fitted: PD150 injectors -- this version is
    set up for: stock injectors" (or "... is not set up for it"); details
    = one :func:`describe` line per record. ``("", "")`` when nothing is
    recorded."""
    st = status(project, sid, cid, unsaved=unsaved)
    cur, cal, dif = st["fitted"], st["set_up_for"], st["differ"]
    if not cur:
        return "", ""
    line = "Fitted: %s" % summaries(cur)
    if dif:
        other = [r for r in cal if r.kind in dif]
        line += (" — this version is set up for: %s" % summaries(other) if other
                 else " — this version is not set up for it")
    tip = ["Fitted to the car:"] + ["  %s: %s" % (r.kind_label, describe(r, project, sid))
                                    for r in cur]
    if dif:
        tip.append("This version is set up for:")
        tip += ["  %s: %s" % (r.kind_label, describe(r, project, sid)) for r in cal] or \
            ["  what the car came with (no setup recorded in its history)"]
    else:
        tip.append("This version is set up for it.")
    return line, "\n".join(tip)


# ------------------------------------------------------------------ writing
def _store(project, sid: str, recs: List[dict]) -> None:
    project.set(sid).extra[KEY] = recs
    project._touch()


def add(project, sid: str, kind: str, summary: str, parts: Optional[dict] = None, *,
        since: Optional[str] = None, checkpoint: Optional[str] = None,
        pending: bool = False, branch: Optional[str] = None,
        source: str = FROM_USER, note: str = "") -> Fitted:
    if not kind or not summary.strip():
        raise ValueError("a hardware record needs a kind and a summary")
    if checkpoint is not None and project.set(sid).checkpoint(checkpoint) is None:
        raise ValueError("no version %r in this set" % checkpoint)
    rec = Fitted(id=new_id(), kind=kind, summary=summary.strip(), parts=dict(parts or {}),
                 since=since or now(), checkpoint=checkpoint, pending=bool(pending),
                 branch=branch, source=source, note=note.strip(), recorded=now())
    _store(project, sid, list(_raw(project, sid)) + [rec.to_dict()])
    return rec


def update(project, sid: str, rid: str, **fields) -> Fitted:
    raw = list(_raw(project, sid))
    for i, d in enumerate(raw):
        if isinstance(d, dict) and d.get("id") == rid:
            rec = Fitted.from_dict(d)
            for k, v in fields.items():
                if k not in Fitted._FIELDS or k == "id":
                    raise ValueError("unknown hardware field %r" % k)
                setattr(rec, k, v)
            raw[i] = rec.to_dict()
            _store(project, sid, raw)
            return rec
    raise KeyError(rid)


def remove(project, sid: str, rid: str) -> None:
    raw = [d for d in _raw(project, sid) if not (isinstance(d, dict) and d.get("id") == rid)]
    if len(raw) == len(_raw(project, sid)):
        raise KeyError(rid)
    _store(project, sid, raw)


def record_setup(project, sid: str, bid: str, kind: str, summary: str, parts: dict, *,
                 unsaved: bool = True, source: str = FROM_SETUP,
                 note: str = "") -> Optional[Fitted]:
    """What a setup tool calls when it writes a calibration for new hardware
    into branch ``bid``'s tab: a record anchored on the branch head
    (``unsaved``: the calibration is in the tab's edits, pending). None when
    the latest record of that kind already says the same about this
    version (applying the same setup again)."""
    b = project.set(sid).branch(bid)
    head = b.head if b is not None else None
    last = for_checkpoint(project, sid, head, kind, unsaved=True)
    if last and last[0].parts == parts:
        return None
    # recorded as fitted before the calibration was made for it: that
    # record now gets its calibration (its date stays the fitting date)
    now_ = current(project, sid, kind)
    if now_ and now_[0].parts == parts and not now_[0].checkpoint:
        return update(project, sid, now_[0].id, checkpoint=head, pending=unsaved,
                      branch=b.id if b is not None else None)
    return add(project, sid, kind, summary, parts, checkpoint=head, pending=unsaved,
               branch=b.id if b is not None else None, source=source, note=note)


def record_fitted(project, sid: str, bid: str, kind: str, summary: str, parts: dict, *,
                  calibrated: bool, unsaved: bool = False, since: Optional[str] = None,
                  source: str = FROM_USER, note: str = "") -> Fitted:
    """"The car has this now", said from a setup tool without writing a
    calibration: anchored on the branch head when the file there is already
    calibrated for it (``calibrated``; ``unsaved`` when that is in edits
    not saved yet), otherwise with no calibration yet."""
    b = project.set(sid).branch(bid)
    head = b.head if (b is not None and calibrated) else None
    return add(project, sid, kind, summary, parts, since=since, checkpoint=head,
               pending=bool(calibrated and unsaved),
               branch=b.id if b is not None else None, source=source, note=note)


__all__ = ["Fitted", "KEY", "VE_PUMP", "PD_INJECTORS", "KIND_LABELS",
           "FROM_SETUP", "FROM_USER", "FROM_AI",
           "records", "get", "current", "at", "for_checkpoint", "describe", "differ",
           "split_logs", "report", "setup_changes", "summaries", "status", "status_text",
           "add", "update", "remove", "record_setup", "record_fitted"]
