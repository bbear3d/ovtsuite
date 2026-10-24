# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A watch folder: every file that arrives is converted with one job.

The job is the converter's own (a preset, or the window's settings). Its
files stay fixed; each arriving file takes the role its kind has there:

* a **binary** next to a fixed definition is the job's binary -- drop
  binaries, get each one's values (DCM / CDFX), each one with the fixed
  value files written in, or each one's ported XDF;
* otherwise an arriving file is a **source**: definitions, value files or
  images converted to the job's target.

A file is taken once it has stopped changing (size and time the same for
``settle`` seconds), so a copy still in progress is never read. Results go
to the output folder (never the watched folder itself); the arriving file is
left where it is or moved to a ``Done`` folder. A result already in the
output folder is replaced only when the job says so (``options["existing"]``
``"overwrite"``); with ``"ask"`` -- there is no one to ask -- or ``"skip"``
it is left alone and the arriving file reported as skipped. A result this
watch itself wrote, from the same arriving file, is its own and is always
replaced when that file changes and arrives again. Polling, not file-system
events: it works the same on every system and on network folders, where
change notifications are unreliable.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import DEF_OUT, DIFF, EXT, IMG, NAMES, VAL, ConvertError, Job, run, sniff, sniff_file

#: names never taken: partial downloads, editors' lock files, our reports
_SKIP_SUFFIX = (".tmp", ".part", ".partial", ".crdownload", ".download", ".convert.txt",
                ".lock", ".swp")
_SKIP_PREFIX = (".", "~$", "~")
DONE = "Done"
OUTPUT = "Converted"


@dataclass
class Event:
    """What happened to one arriving file."""
    path: str
    #: True converted, False failed, None not for this job (skipped)
    ok: Optional[bool]
    text: str
    out: List[str] = field(default_factory=list)
    lossy: bool = False
    when: float = 0.0


def _kind(path: str) -> Tuple[str, str]:
    return sniff_file(path)


class Watcher:
    def __init__(self, folder: str, job: Job, out_dir: Optional[str] = None,
                 after: str = "leave", existing: bool = False, recursive: bool = False,
                 settle: float = 2.0, clock: Callable[[], float] = time.time):
        if not os.path.isdir(folder):
            raise ConvertError("No such folder: %s" % folder)
        if (job.target or "").lower() == DIFF:
            raise ConvertError("A comparison needs two definitions; it cannot run on a "
                               "watch folder.")
        if (job.target or "").lower() not in DEF_OUT + IMG + VAL:
            raise ConvertError("Choose what to convert to first.")
        if after not in ("leave", "move"):
            raise ConvertError("After converting: 'leave' or 'move'.")
        self.folder = os.path.abspath(folder)
        self.job = Job.from_dict(job.to_dict())
        self.job.sources = [os.path.abspath(p) for p in job.sources]
        self.out_dir = os.path.abspath(out_dir or os.path.join(self.folder, OUTPUT))
        if os.path.normcase(self.out_dir) == os.path.normcase(self.folder):
            raise ConvertError("The output folder must not be the watched folder: every "
                               "result would arrive as a new file to convert.")
        self.done_dir = os.path.join(self.folder, DONE)
        self.after, self.recursive, self.settle, self.clock = after, recursive, settle, clock
        self._fixed = {}
        for p in self.job.sources:
            try:
                self._fixed[p] = _kind(p)[0]
            except OSError as exc:
                raise ConvertError("%s: %s" % (p, exc))
        #: path -> (size, mtime, first time it looked like this)
        self._seen: Dict[str, Tuple[int, float, float]] = {}
        #: (path, size, mtime) already handled
        self._handled = set()
        self._ours = set()            # files this watch wrote or moved
        self._made: Dict[str, str] = {}   # result -> the arrival it came from
        if not existing:
            for p, st in self._scan():
                self._handled.add((p, st.st_size, st.st_mtime))

    # ------------------------------------------------------------- scanning
    def _ignored_dir(self, d: str) -> bool:
        n = os.path.normcase(os.path.abspath(d))
        for x in (self.out_dir, self.done_dir):
            x = os.path.normcase(x)
            if n == x or n.startswith(x + os.sep):
                return True
        return False

    def _scan(self):
        if self.recursive:
            walk = os.walk(self.folder)
        else:
            try:
                walk = [(self.folder, [], os.listdir(self.folder))]
            except OSError:
                return
        for d, dirs, names in walk:
            dirs[:] = [x for x in dirs if not x.startswith(".")
                       and not self._ignored_dir(os.path.join(d, x))]
            if self._ignored_dir(d):
                continue
            for n in sorted(names):
                low = n.lower()
                if low.startswith(_SKIP_PREFIX) or low.endswith(_SKIP_SUFFIX):
                    continue
                p = os.path.join(d, n)
                if p in self._fixed or os.path.normcase(p) in self._ours:
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not os.path.isfile(p) or st.st_size == 0:
                    continue
                yield p, st

    def ready(self) -> List[str]:
        """Files that arrived and have stopped changing."""
        now = self.clock()
        out, live = [], set()
        for p, st in self._scan():
            live.add(p)
            if (p, st.st_size, st.st_mtime) in self._handled:
                continue
            prev = self._seen.get(p)
            if prev is None or prev[:2] != (st.st_size, st.st_mtime):
                self._seen[p] = (st.st_size, st.st_mtime, now)
                continue
            if now - prev[2] >= self.settle:
                out.append(p)
        for p in list(self._seen):
            if p not in live:
                del self._seen[p]
        return out

    def roles(self) -> Dict[str, str]:
        """What happens to each kind of arriving file, in words (the rules
        of :meth:`job_for`)."""
        return roles(self.job, set(self._fixed.values()))

    # ------------------------------------------------------------ converting
    def job_for(self, path: str) -> Tuple[Optional[Job], str]:
        """The job for one arriving file, or ``(None, why not)``."""
        try:
            kind, fmt = _kind(path)
        except OSError as exc:
            return None, str(exc)
        t = self.job.target.lower()
        fixed = set(self._fixed.values())
        j = Job.from_dict(self.job.to_dict())
        j.options = dict(self.job.options)
        stem = os.path.splitext(os.path.basename(path))[0]
        if kind == "image" and "definition" in fixed and (
                t not in IMG or "values" in fixed):
            j.binary = path                       # the job's binary
        elif kind == "image" and t in IMG and not fixed:
            j.sources = [path]
        elif kind == "definition" and (t in DEF_OUT or (t in VAL and j.binary)):
            j.sources = j.sources + [path]
        elif kind == "values" and (t in VAL or (t in IMG and "definition" in fixed
                                                and j.binary)):
            j.sources = j.sources + [path]
        else:
            return None, "a %s is not used by this job" % (
                {"image": "binary", "values": "value file"}.get(kind, kind))
        out = os.path.join(self.out_dir, stem + EXT[t])
        if os.path.exists(out) and self._made.get(os.path.normcase(out)) not in (None, path):
            # a.bin and a.hex arriving together: each keeps its own result
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            out = os.path.join(self.out_dir, "%s-%s%s" % (stem, ext, EXT[t]))
        j.out = out
        return j, ""

    def convert(self, path: str) -> Event:
        try:
            st = os.stat(path)
            self._handled.add((path, st.st_size, st.st_mtime))
        except OSError:
            pass
        self._seen.pop(path, None)
        now = self.clock()
        j, why = self.job_for(path)
        if j is None:
            return Event(path, None, "skipped: " + why, when=now)
        os.makedirs(self.out_dir, exist_ok=True)
        if self._made.get(os.path.normcase(j.out)) == path:
            j.options["existing"] = "overwrite"       # this watch's own result
        try:
            res = run(j)
        except (ConvertError, ValueError, OSError) as exc:
            return Event(path, False, str(exc), when=now)
        except Exception as exc:                                # noqa: BLE001
            return Event(path, False, "%s: %s" % (type(exc).__name__, exc), when=now)
        if res.skipped:
            return Event(path, None, "skipped: %s is already there (not overwritten)"
                         % os.path.basename(j.out), when=now)
        self._made[os.path.normcase(j.out)] = path
        for o in res.out:
            self._ours.add(os.path.normcase(os.path.abspath(o)))
        bits = ["%d tables" % res.tables_out] if res.tables_out else []
        if res.values:
            bits.append(res.values[0])
        if res.loss:
            bits.append("%d not expressed fully (report beside it)" % len(res.loss))
        if res.check:
            bits.append("read-back check: %d differences" % len(res.check))
        if self.after == "move":
            self._move_done(path)
        return Event(path, True, "; ".join(bits) or "done", list(res.out), res.lossy, now)

    def _move_done(self, path: str) -> None:
        os.makedirs(self.done_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(path))
        dest, k = os.path.join(self.done_dir, base + ext), 1
        while os.path.exists(dest):
            k += 1
            dest = os.path.join(self.done_dir, "%s (%d)%s" % (base, k, ext))
        try:
            shutil.move(path, dest)
        except OSError:
            pass

    def poll(self) -> List[Event]:
        """Convert every file that is ready; what happened to each."""
        return [self.convert(p) for p in self.ready()]


def roles(job: Job, fixed) -> Dict[str, str]:
    """``{"definition"|"values"|"image": what happens to one}`` for a job
    whose fixed files are of the kinds ``fixed``."""
    t = (job.target or "").lower()
    name = NAMES.get(t, t)
    out = {}
    if "definition" in fixed:
        out["image"] = ("used as the binary: %s written" % name if t in VAL else
                        "used as the binary: the value files written into it (%s)" % name
                        if t in IMG and "values" in fixed else
                        "used as the binary: the definition written as %s" % name
                        if t in DEF_OUT else "not used")
    else:
        out["image"] = "converted to %s" % name if t in IMG else "not used"
    out["definition"] = ("converted to %s" % name if t in DEF_OUT else
                         "its values read from the binary (%s)" % name
                         if t in VAL and job.binary else "not used")
    out["values"] = ("converted to %s" % name if t in VAL else
                     "written into the binary (%s)" % name
                     if t in IMG and "definition" in fixed and job.binary else "not used")
    return out


def loop(w: Watcher, say: Callable[[str], None] = print, interval: float = 1.0,
         stop: Callable[[], bool] = lambda: False) -> None:
    """Watch until ``stop()`` (or Ctrl+C)."""
    say("Watching %s -> %s (Ctrl+C to stop)" % (w.folder, w.out_dir))
    try:
        while not stop():
            for e in w.poll():
                mark = {True: "ok  ", False: "FAIL", None: "skip"}[e.ok]
                say("%s %s: %s" % (mark, os.path.basename(e.path), e.text))
            time.sleep(interval)
    except KeyboardInterrupt:
        say("Stopped.")
