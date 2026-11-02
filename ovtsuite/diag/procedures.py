# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Guided procedures: a ``procedure`` definition run step by step against a
live module session, with the user prompted where the definition says.

A procedure is data (schema doc §4.9): ``read`` / ``choose`` / ``write`` /
``live`` / ``check`` / ``show`` / ``wait`` / ``log`` steps. The engine here
turns each into session calls and :class:`Prompter` calls; it never
decides a write by itself -- a write step shows what will be written and
needs the user's yes, is journalled, and is verified by re-reading unless
the definition says ``verify: none`` (with a reason).

Values a step produces are kept by name (``read`` results under their
entry ids, a ``choose`` under its ``id``), so a later step can refer to
them. Adaptation channels are addressed by entry id (``adaptation.ch02``,
or the pack's own id for that channel); measuring groups by number.

Preconditions (``check``) are tested from what the module says where that
is possible -- engine speed from a cell whose formula is rpm, faults from
the module's own list -- and put to the user as a yes/no where it is not.

Qt-free: the window gives it a prompter that blocks the worker thread on
the GUI's answer; the tests give it a scripted one.
"""
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .defs import Resolver, as_int
from .defs.model import Identity

Log = Callable[[str], None]
_CH_RE = re.compile(r"^adaptation\.ch(\d+)$")


class Cancelled(Exception):
    """The user stopped the procedure."""


class ProcedureError(Exception):
    """A step could not be done (a channel the definitions do not know, a
    verification that failed)."""


class Prompter:
    """What the engine asks of the user. Every call blocks until answered.
    The base class answers like an unattended run: no choices, no writes."""

    def choose(self, step_id: str, label: str, options: Dict[str, str], default: Optional[str] = None) -> Optional[str]:
        return default

    def confirm(self, text: str, detail: str = "") -> bool:
        return False

    def show(self, text: str, rows: Optional[List[List[str]]] = None) -> bool:
        """Show ``text`` (and ``rows``) until the user continues; False when
        the user stopped instead (the engine cancels the run)."""
        return True

    def live(self, title: str, read: Callable[[], List[List[str]]], seconds: float, text: str = "") -> bool:
        """Call ``read()`` repeatedly for ``seconds`` (or until the user
        stops), showing the rows; False when the user cancelled."""
        end = time.time() + seconds
        while time.time() < end:
            read()
        return True

    def wait(self, text: str, seconds: float) -> bool:
        time.sleep(seconds)
        return True

    def progress(self, step_index: int, total: int, text: str):
        pass


@dataclass
class Write:
    entry_id: str
    channel: int
    before: Optional[int]
    wanted: int
    after: Optional[int] = None

    @property
    def verified(self) -> Optional[bool]:
        return None if self.after is None else self.after == self.wanted


@dataclass
class RunResult:
    procedure_id: str
    name: str
    status: str = "done"               # done | cancelled | failed | blocked
    message: str = ""
    values: Dict[str, Any] = field(default_factory=dict)
    writes: List[Write] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done"


def applies(doc: Dict[str, Any], ident: Optional[Identity]) -> bool:
    """Whether a procedure document applies to ``ident``: ``applies.addr``,
    ``applies.platform`` (one or several), ``applies.protocol``."""
    a = doc.get("applies") or {}
    if ident is None:
        return True
    from .defs.model import _norm_addr
    if a.get("addr") is not None and ident.addr is not None:
        if _norm_addr(a.get("addr")) != _norm_addr(ident.addr):
            return False
    plats = a.get("platform")
    if plats and ident.platform:
        plats = plats if isinstance(plats, list) else [plats]
        if str(ident.platform).lower() not in [str(p).lower() for p in plats]:
            return False
    proto = a.get("protocol")
    if proto and ident.protocol and str(proto).lower() != str(ident.protocol).lower():
        return False
    return True


def available(resolver: Resolver, ident: Optional[Identity]) -> List[Dict[str, Any]]:
    """The procedure documents that apply to ``ident`` (their bodies, with
    ``_pack`` set), those needing definitions the identity lacks left out."""
    out = []
    for r in resolver.procedures(None):
        body = dict(r.entry)
        if not applies(body, ident):
            continue
        body["_pack"] = r.pack_id
        req = (body.get("applies") or {}).get("requires") or []
        if req and ident is not None:
            have = set(resolver.view("adaptation", ident).entries)
            if any(x not in have and not _CH_RE.match(str(x)) for x in req):
                continue
        out.append(body)
    return out


class ProcedureRun:
    def __init__(self, doc: Dict[str, Any], sess, resolver: Optional[Resolver], prompter: Prompter,
                 journal: Optional[Callable[..., Any]] = None, log: Optional[Log] = None,
                 wsc: Optional[int] = None):
        self.doc = doc
        self.sess = sess
        self.resolver = resolver
        self.p = prompter
        self.journal = journal
        self.log = log or (lambda s: None)
        self.wsc = wsc
        self.result = RunResult(procedure_id=str(doc.get("id", "")), name=str(doc.get("name", "")))
        self.values: Dict[str, Any] = self.result.values

    # ------------------------------------------------------------ running
    def run(self) -> RunResult:
        steps = self.doc.get("steps") or []
        r = self.result
        try:
            for i, step in enumerate(steps):
                kind, spec = next(iter(step.items()))
                self.p.progress(i, len(steps), kind)
                fn = getattr(self, "_step_" + kind, None)
                if fn is None:
                    raise ProcedureError("step %d: unknown step kind %r" % (i + 1, kind))
                fn(spec if spec is not None else {})
            r.status = "done"
        except Cancelled as ex:
            r.status = "cancelled"
            r.message = str(ex) or "cancelled"
        except ProcedureError as ex:
            r.status = "failed"
            r.message = str(ex)
        except Exception as ex:
            r.status = "failed"
            r.message = "%s: %s" % (type(ex).__name__, ex)
        r.finished = time.time()
        self.log("Procedure %s: %s%s" % (r.name or r.procedure_id, r.status, (" (%s)" % r.message) if r.message else ""))
        return r

    # ------------------------------------------------------------- helpers
    def _ident(self) -> Identity:
        return self.sess.ident

    def _channel_of(self, entry_id: str) -> int:
        """The adaptation channel number an entry id names."""
        m = _CH_RE.match(str(entry_id))
        if self.resolver is not None:
            v = self.resolver.view("adaptation", self._ident())
            r = v.entries.get(str(entry_id))
            if r is not None and as_int(r.entry.get("channel"), None) is not None:
                return as_int(r.entry.get("channel"))
        if m:
            return int(m.group(1))
        raise ProcedureError("the definitions for this module have no adaptation entry %r" % entry_id)

    def _entry_name(self, entry_id: str) -> str:
        if self.resolver is not None:
            r = self.resolver.view("adaptation", self._ident()).entries.get(str(entry_id))
            if r is not None:
                return str(r.entry.get("name", "")) or str(entry_id)
        return str(entry_id)

    def _group_rows(self, group: int) -> List[List[str]]:
        reading = self.sess.read_group_full(group)
        if reading is None or not getattr(reading, "available", False) or reading.cells is None:
            return [["%03d" % group, "", "not available", ""]]
        names = self.sess.group_names(group)
        return [["%03d.%d" % (group, i + 1), (names[i] if i < len(names) and names[i] else ""),
                 c.decoded[2], "%02X %02X %02X" % (c.fid, c.a, c.b)] for i, c in enumerate(reading.cells)]

    def _groups(self, spec) -> List[int]:
        g = spec.get("groups")
        if g == "catalogue" or g == "all":
            namer = getattr(self.sess, "catalogue_namer", None)
            if namer is not None:
                return list(namer.populated_groups())
            return list(range(1, 26))
        if g is None and spec.get("group") is not None:
            return [as_int(spec.get("group"))]
        return [as_int(x) for x in (g or []) if as_int(x, None) is not None]

    def _rpm(self) -> Optional[float]:
        """Engine speed from group 1's rpm-formula cell, None when the
        module serves none."""
        try:
            reading = self.sess.read_group_full(1)
        except Exception:
            return None
        if reading is None or not getattr(reading, "available", False) or not reading.cells:
            return None
        for c in reading.cells:
            v, unit, _t = c.decoded
            if unit == "rpm" and v is not None:
                return float(v)
        return None

    # --------------------------------------------------------------- steps
    def _step_read(self, spec):
        rows: List[List[str]] = []
        if spec.get("faults"):
            faults = self.sess.read_faults()
            self.values["faults"] = ["%s %s" % (f.display_code, f.text) for f in faults]
            rows += [[f.display_code, f.text or "", f.status_text or "", " | ".join(str(v) for v in f.freeze.values())]
                     for f in faults] or [["", "no stored faults", "", ""]]
        for eid in spec.get("channels") or []:
            ch = self._channel_of(eid)
            a = self.sess.adaptation_read(ch)
            self.values[str(eid)] = a.value
            rows.append(["%03d" % ch, self._entry_name(eid), str(a.value), ""])
        for g in self._groups(spec):
            rs = self._group_rows(g)
            self.values["group.%d" % g] = rs
            rows += rs
        if rows:
            self.p.show(spec.get("text") or "Read from the module:", rows)

    def _step_choose(self, spec):
        sid = str(spec.get("id") or "choice")
        opts = spec.get("options") or {}
        labels = {str(k): str((v or {}).get("label", k)) if isinstance(v, dict) else str(v) for k, v in opts.items()}
        key = self.p.choose(sid, str(spec.get("label") or spec.get("text") or sid), labels, spec.get("default"))
        if key is None or str(key) not in labels:
            raise Cancelled("no choice made")
        self.values[sid] = str(key)
        self.values["_choice_" + sid] = opts.get(key) or opts.get(str(key)) or {}

    def _step_write(self, spec):
        writes: Dict[str, Any] = dict(spec.get("writes") or {})
        src = spec.get("from")
        if src:
            chosen = self.values.get("_choice_" + str(src)) or {}
            writes.update((chosen.get("writes") or {}) if isinstance(chosen, dict) else {})
        if not writes:
            raise ProcedureError("write step names nothing to write")
        plan = []
        for eid, val in writes.items():
            ch = self._channel_of(eid)
            try:
                before = self.sess.adaptation_read(ch).value
            except Exception:
                before = None
            plan.append(Write(str(eid), ch, before, int(as_int(val, 0))))
        detail = "\n".join("channel %03d %s: %s -> %d" % (w.channel, self._entry_name(w.entry_id),
                                                        "-" if w.before is None else w.before, w.wanted)
                           for w in plan)
        if not self.p.confirm("Write %d adaptation value%s to %s %s?" % (
                len(plan), "" if len(plan) == 1 else "s", self.sess.addr, self.sess.info.name), detail):
            raise Cancelled("write not confirmed")
        verify = str(spec.get("verify") or "reread")
        for w in plan:
            self.sess.adaptation_save(w.channel, w.wanted, self.wsc)
            if verify != "none":
                w.after = self.sess.adaptation_read(w.channel).value
            self.result.writes.append(w)
            if self.journal is not None:
                self.journal(addr=self.sess.addr, module=self.sess.info.name, function="adaptation",
                             entry_id=w.entry_id, before=w.before, after=w.after if w.after is not None else w.wanted,
                             verified=w.verified, note="procedure %s" % (self.result.name or self.result.procedure_id))
            if verify != "none" and w.after != w.wanted:
                raise ProcedureError("channel %03d reads %s after the write, not %d" % (w.channel, w.after, w.wanted))

    def _step_live(self, spec):
        groups = self._groups(spec)
        if not groups:
            raise ProcedureError("live step names no group")
        seconds = float(spec.get("seconds") or 15)
        title = str(spec.get("title") or "Live: group%s %s" % ("" if len(groups) == 1 else "s", ", ".join("%03d" % g for g in groups)))
        last: Dict[int, List[List[str]]] = {}

        def read():
            rows: List[List[str]] = []
            for g in groups:
                last[g] = self._group_rows(g)
                rows += last[g]
            return rows

        if not self.p.live(title, read, seconds, str(spec.get("text") or "")):
            raise Cancelled("stopped during the live step")
        for g, rs in last.items():
            self.values["live.%d" % g] = rs

    def _step_check(self, spec):
        items = spec if isinstance(spec, list) else [spec]
        for item in items:
            if isinstance(item, dict):
                for k, v in item.items():
                    self._check_one(str(k), v)
            else:
                self._check_one(str(item), None)

    def _check_one(self, name: str, arg):
        if name in ("engine_off", "engine_running"):
            rpm = self._rpm()
            if rpm is None:
                if not self.p.confirm("The module does not report engine speed. Is the engine %s?"
                                      % ("off (ignition on)" if name == "engine_off" else "running")):
                    raise Cancelled("precondition %s not met" % name)
                return
            ok = (rpm < 50) if name == "engine_off" else (rpm >= 300)
            if not ok:
                raise ProcedureError("precondition %s: engine speed is %.0f rpm" % (name, rpm))
            self.result.notes.append("%s: %.0f rpm" % (name, rpm))
            return
        if name == "ignition_on":
            return                                      # the session is open: the ignition is on
        if name == "no_faults_in":
            addrs = [str(a).upper() for a in (arg if isinstance(arg, list) else [arg] if arg else [])]
            if not addrs or self.sess.addr.upper() in addrs:
                faults = self.sess.read_faults()
                if faults:
                    raise ProcedureError("the module has %d stored fault%s; clear them first"
                                         % (len(faults), "" if len(faults) == 1 else "s"))
            others = [a for a in addrs if a != self.sess.addr.upper()]
            if others and not self.p.confirm("Confirm that module%s %s %s no stored faults (check them in the tree)."
                                             % ("" if len(others) == 1 else "s", ", ".join(others),
                                                "has" if len(others) == 1 else "have")):
                raise Cancelled("precondition no_faults_in not confirmed")
            return
        if name == "coolant_above_c":
            if not self.p.confirm("Is the coolant above %s °C?" % arg):
                raise Cancelled("precondition coolant_above_c not met")
            return
        if name == "vehicle_stationary":
            if not self.p.confirm("Is the vehicle stationary with the handbrake on?"):
                raise Cancelled("precondition vehicle_stationary not met")
            return
        raise ProcedureError("unknown precondition %r" % name)

    def _step_show(self, spec):
        text = spec if isinstance(spec, str) else str(spec.get("text") or "")
        rows = None
        if isinstance(spec, dict) and spec.get("values"):
            rows = []
            for key in spec["values"]:
                v = self.values.get(str(key))
                if isinstance(v, list):
                    rows += [list(r) for r in v]
                else:
                    rows.append([str(key), "", "" if v is None else str(v), ""])
        # only an explicit False is a stop: a prompter that returns nothing
        # (older subclasses) continues
        if self.p.show(text, rows) is False:
            raise Cancelled("stopped")

    def _step_wait(self, spec):
        seconds = float(spec.get("seconds") or 1) if isinstance(spec, dict) else float(spec or 1)
        text = str(spec.get("text") or "Waiting %.0f s..." % seconds) if isinstance(spec, dict) else "Waiting..."
        if not self.p.wait(text, seconds):
            raise Cancelled("stopped while waiting")

    def _step_log(self, spec):
        if self.journal is None or not (spec is True or (isinstance(spec, dict) and spec.get("journal", True))):
            return
        note = spec.get("note") if isinstance(spec, dict) else ""
        summary = {k: v for k, v in self.values.items() if not str(k).startswith("_") and not isinstance(v, list)}
        self.journal(addr=self.sess.addr, module=self.sess.info.name, function="procedure",
                     entry_id=self.result.procedure_id, before=None, after=summary or None,
                     verified=None, note=note or (self.result.name or self.result.procedure_id))


__all__ = ["ProcedureRun", "Prompter", "RunResult", "Write", "Cancelled", "ProcedureError", "available", "applies"]
