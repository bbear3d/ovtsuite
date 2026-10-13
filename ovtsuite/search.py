# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Parameter search: matching, ranking, and address / value / byte scans.

Everything here is Qt-free so it can be tested without a display and run on a
worker thread. The GUI half lives in :mod:`ovtsuite.gui_search`.

Four ways to look, one engine (:class:`SearchEngine`):

``text``
    Names, descriptions, categories, units. Exact (substring), fuzzy (ranked
    subsequence, several words in any order) or regular expression, with case
    and whole-word switches.
``value``
    Parameters whose *stored data* matches a number, a range or a comparison,
    in display units or in the raw stored value.
``address``
    Which parameter owns an address -- including an address in the middle of
    one, which resolves to the exact cell or axis element. Uses the entities'
    own placed extents (Z data, X axis, Y axis); nothing is guessed.
``bytes``
    A byte pattern (hex with ``??`` wildcards, ASCII, an integer, a float) found
    inside one chosen parameter's extents. Searching the whole image is the hex
    editor's job and is deliberately not duplicated here.

Searches take a :class:`Cancel` token and an optional progress callback, and
may span several :class:`Source` objects (open tabs, or a tab's primary and
comparison).
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import math
import os
import re
import struct
import threading
import weakref
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MODES = ("text", "value", "address", "bytes")
MATCHES = ("fuzzy", "exact", "regex")

#: text fields and how much a hit in each is worth when ranking
FIELD_WEIGHTS = {
    "symbol": 1.0,
    "title": 0.95,
    "label": 0.9,
    "category": 0.5,
    "description": 0.4,
    "units": 0.3,
}
ALL_FIELDS = tuple(FIELD_WEIGHTS)
DEFAULT_FIELDS = ("symbol", "title", "category", "description", "units")

#: cap on cells recorded per hit (highlighting, display); counts stay exact
MAX_CELLS = 4096

FORMAT = 2                      # bump when the shipped index layout or key changes
INDEX_FILE = "search_index.json.gz"


class QueryError(ValueError):
    """The query itself is unusable (bad regex, bad number, bad hex...). The
    message is shown to the person, so it says what to fix."""


class Cancelled(Exception):
    """Raised inside a search when its :class:`Cancel` token is set."""


class Cancel:
    """Cooperative cancel flag, safe to set from another thread."""

    def __init__(self):
        self._ev = threading.Event()

    def set(self):
        self._ev.set()

    @property
    def is_set(self) -> bool:
        return self._ev.is_set()

    def check(self):
        if self._ev.is_set():
            raise Cancelled()


_NEVER = Cancel()


# --------------------------------------------------------------- options --
@dataclass
class SearchOptions:
    mode: str = "text"
    # -- text
    match: str = "fuzzy"                    # fuzzy | exact | regex
    case_sensitive: bool = False
    whole_word: bool = False
    fields: Tuple[str, ...] = DEFAULT_FIELDS
    tier: int = 0                           # 0 = any
    # -- value
    raw_values: bool = False                # compare the stored value
    tolerance: float = 0.0                  # +/- around an exact number
    include_axes: bool = True               # value + bytes: axes as well as Z
    # -- address
    base: int = 16                          # how a bare number is read
    # -- bytes
    bytes_kind: str = "hex"                 # hex | ascii | int | float
    width: int = 16                         # int: 8 | 16 | 32 | 64; float: 32 | 64
    signed: bool = False
    endian: str = "auto"                    # auto | big | little
    # -- all
    max_results: int = 5000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fields"] = list(self.fields)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SearchOptions":
        o = cls()
        for k, v in (d or {}).items():
            if hasattr(o, k):
                setattr(o, k, tuple(v) if k == "fields" else v)
        if o.mode not in MODES:
            o.mode = "text"
        if o.match not in MATCHES:
            o.match = "fuzzy"
        o.fields = tuple(f for f in o.fields if f in FIELD_WEIGHTS) \
            or DEFAULT_FIELDS
        return o


@dataclass
class Source:
    """One thing to search: a loaded session, and what to call it."""
    label: str
    session: object
    owner: object = None            # opaque to this module (the GUI's tab)
    role: str = "primary"           # primary | comparison


@dataclass
class Hit:
    entity: object
    source: Source
    score: float = 0.0
    where: str = ""                 # symbol / title / description / Z / X axis ...
    detail: str = ""                # what matched, in words
    addr: Optional[int] = None      # the matching address, for address/bytes
    #: cells to highlight: ("z", r, c) | ("x", i, 0) | ("y", i, 0)
    cells: Tuple[tuple, ...] = ()
    n_cells: int = 0                # true count (cells may be capped)


# ------------------------------------------------------------ text match --
_BOUND_L = r"(?<![A-Za-z0-9])"
_BOUND_R = r"(?![A-Za-z0-9])"


def char_mask(s: str) -> int:
    """Bit set of the characters in ``s`` (ASCII by value, the rest folded
    into one bit). A cheap necessary condition for a subsequence match."""
    m = 0
    for c in set(s):
        o = ord(c)
        m |= 1 << (o if o < 128 else 127)
    return m


def _is_word_start(raw: str, p: int) -> bool:
    if p == 0:
        return True
    prev, cur = raw[p - 1], raw[p]
    if not prev.isalnum():
        return True
    return prev.islower() and cur.isupper()        # camelCase hump


#: how far apart a subsequence match may spread, per letter of the word
MAX_SCATTER = 3

#: fields short enough for a scattered (abbreviation-style) match to mean
#: something; the long ones -- description, category, units -- only match
#: where the word is really there
FUZZY_FIELDS = ("symbol", "title", "label")


def substring_score(token: str, low: str, raw: str) -> Optional[float]:
    j = low.find(token)
    if j < 0:
        return None
    return 8.0 + (3.0 if _is_word_start(raw, j) else 0.0)


def fuzzy_score(token: str, low: str, raw: str, tmask: int,
                fmask: int) -> Optional[float]:
    """Ranked subsequence match of ``token`` in ``low`` (same length as
    ``raw``, which supplies the case for hump detection). None = no match."""
    if tmask & ~fmask:
        return None
    n = len(token)
    j = low.find(token)
    if j >= 0:                              # contiguous beats any scatter
        pos = list(range(j, j + n))
        end = j + n - 1
    else:
        i = -1
        for ch in token:
            i = low.find(ch, i + 1)
            if i < 0:
                return None
        end = i
        pos = [end]
        k = end
        for ch in reversed(token[:-1]):     # tighten: latest start that fits
            k = low.rfind(ch, 0, k)
            pos.append(k)
        pos.reverse()
        if end - pos[0] + 1 > MAX_SCATTER * n + 2:
            return None                     # letters strewn across the text
    score = 0.0
    prev = -2
    for p in pos:
        s = 1.0
        if p == prev + 1:
            s += 2.0
        if _is_word_start(raw, p):
            s += 3.0
        score += s
        prev = p
    window = end - pos[0] + 1
    score -= (window - n) * 0.5
    if pos[0] == 0:
        score += 4.0
    if window == n:
        score += 4.0                        # contiguous
    return max(score, 0.1)


class _Doc:
    """One searchable parameter: its text fields, lowered and masked once."""
    __slots__ = ("ent", "f")

    def __init__(self, ent, fields):
        self.ent = ent
        #: name -> (raw, lowered, mask)
        self.f = {}
        for name, raw in fields:
            if raw:
                low = raw.lower()
                if len(low) != len(raw):
                    low = raw
                self.f[name] = (raw, low, char_mask(low))


class Corpus:
    """The searchable text of one session's parameters."""

    def __init__(self, docs: List[_Doc]):
        self.docs = docs

    @classmethod
    def build(cls, entities: Sequence, title_fn: Callable,
              tier_name: Callable = str, titles: Optional[Dict[str, str]] = None,
              cancel: Cancel = _NEVER) -> "Corpus":
        docs = []
        for n, e in enumerate(entities):
            if not n & 0x3FF:
                cancel.check()
            title = None
            # catalogue names (and their per-file selector notes) are never
            # in the shipped cache: those labels are made from the entity
            if titles is not None and not getattr(e, "plain", "") \
                    and not getattr(e, "coding", ""):
                title = titles.get(title_key(e))
            if title is None:
                try:
                    title = title_fn(e)
                except Exception:
                    title = e.name
            path = getattr(e, "category_path", None) or ()
            cats = " > ".join(path) if path else "; ".join(e.categories or ())
            label = getattr(e, "label", "") or ""
            docs.append(_Doc(e, (
                ("symbol", e.name or ""),
                ("title", title or ""),
                ("label", label if label not in (e.name, title) else ""),
                ("category", "%s | %s" % (cats, tier_name(e.tier))),
                ("description", e.description or ""),
                ("units", e.units or ""),
            )))
        return cls(docs)


def title_key(e) -> str:
    """Key of the shipped title cache: everything the tree label is made from.

    That is the symbol, the description, the label, and -- for the ZF naming
    layer, which treats identification fields differently -- the first
    category of the entity's path."""
    path = getattr(e, "category_path", None) or ()
    h = hashlib.blake2b(digest_size=8)
    for part in (e.name or "", e.description or "",
                 getattr(e, "label", "") or "", path[0] if path else ""):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


class _TextMatcher:
    def __init__(self, query: str, opts: SearchOptions):
        self.q = query.strip()
        self.o = opts
        self.fields = [f for f in opts.fields if f in FIELD_WEIGHTS]
        if not self.q:
            raise QueryError("Type something to search for.")
        flags = 0 if opts.case_sensitive else re.IGNORECASE
        self.pat = None
        self.tokens: List[str] = []
        self.tpats: List[re.Pattern] = []
        if opts.match == "regex":
            try:
                self.pat = re.compile(self.q, flags)
            except re.error as ex:
                raise QueryError("Bad regular expression: %s" % ex)
        elif opts.match == "exact":
            body = re.escape(self.q)
            if opts.whole_word:
                body = _BOUND_L + body + _BOUND_R
            self.pat = re.compile(body, flags)
        else:                               # fuzzy: words, any order
            self.tokens = self.q.split()
            if opts.whole_word:
                self.tpats = [re.compile(_BOUND_L + re.escape(t) + _BOUND_R,
                                         flags) for t in self.tokens]
            else:
                # masks are of lowered text, so lower the token for the mask
                # even when the comparison itself is case-sensitive
                self.tmasks = [char_mask(t.lower()) for t in self.tokens]
                if not opts.case_sensitive:
                    self.tokens = [t.lower() for t in self.tokens]

    def match(self, d: _Doc):
        """(score, field, snippet) or None."""
        if self.pat is not None:
            best = None
            for name in self.fields:
                ent = d.f.get(name)
                if ent is None:
                    continue
                m = self.pat.search(ent[0])
                if m is None:
                    continue
                s = 50.0 * FIELD_WEIGHTS[name]
                if self.o.match == "exact":
                    s += 20.0 if m.start() == 0 else (
                        10.0 if _is_word_start(ent[0], m.start()) else 0.0)
                s -= min(len(ent[0]), 400) * 0.05
                if best is None or s > best[0]:
                    best = (s, name, _snippet(ent[0], m.start(), m.end()))
            return best
        total = 0.0
        first = None
        for ti, tok in enumerate(self.tokens):
            best = None
            for name in self.fields:
                ent = d.f.get(name)
                if ent is None:
                    continue
                if self.tpats:
                    m = self.tpats[ti].search(ent[0])
                    if m is None:
                        continue
                    s = 20.0 * FIELD_WEIGHTS[name]
                    where = (m.start(), m.end())
                else:
                    text = ent[0] if self.o.case_sensitive else ent[1]
                    if name in FUZZY_FIELDS:
                        s = fuzzy_score(tok, text, ent[0], self.tmasks[ti],
                                        ent[2])
                    else:
                        s = substring_score(tok, text, ent[0])
                    if s is None:
                        continue
                    s *= FIELD_WEIGHTS[name]
                    j = text.find(tok)
                    where = (j, j + len(tok)) if j >= 0 else (0, 0)
                if best is None or s > best[0]:
                    best = (s, name, where)
            if best is None:
                return None                 # every word must be found
            total += best[0]
            if first is None:
                first = (best[1], best[2])
        name, (a, b) = first
        raw = d.f[name][0]
        return (total, name, _snippet(raw, a, b))


def _snippet(text: str, a: int, b: int, width: int = 110) -> str:
    if len(text) <= width:
        return text
    lo = max(0, min(a - width // 3, len(text) - width))
    out = text[lo:lo + width]
    return ("…" if lo else "") + out + ("…" if lo + width < len(text) else "")


# ------------------------------------------------------------ extents ----
@dataclass
class Span:
    lo: int
    hi: int                         # exclusive
    ent: object
    kind: str                       # "z" | "x" | "y"
    step: int

    def cell_at(self, addr: int) -> Tuple[str, int, int]:
        """The ('z'|'x'|'y', a, b) element that holds byte ``addr``."""
        i = (addr - self.lo) // self.step
        if self.kind != "z":
            return (self.kind, i, 0)
        e = self.ent
        if e.kind == "map" and e.order == "col":
            return ("z", i % e.rows, i // e.rows)
        return ("z", i // e.cols, i % e.cols)

    def label(self) -> str:
        return {"z": "data", "x": "X axis", "y": "Y axis"}[self.kind]


def entity_spans(e, axes: bool = True) -> List[Span]:
    """The byte ranges an entity occupies in its binary."""
    out: List[Span] = []
    try:
        if e.addr is not None and e.rows > 0 and e.cols > 0:
            out.append(Span(e.addr, e.end, e, "z", max(1, e.step)))
        if axes:
            for kind, ax in (("x", e.x), ("y", e.y)):
                if ax is not None and ax.addr is not None and ax.count > 0:
                    step = 4 if ax.is_float else max(1, ax.bits // 8)
                    out.append(Span(ax.addr, ax.addr + ax.count * step, e,
                                    kind, step))
    except (AttributeError, TypeError):
        pass
    return out


class AddressIndex:
    """Every placed byte range of one session, ready to answer "who owns this
    address?" in logarithmic time. Independent of edits (addresses do not
    move), so it is built once per loaded file."""

    def __init__(self, spans: List[Span]):
        self.spans = sorted(spans, key=lambda s: (s.lo, s.hi))
        self._los = [s.lo for s in self.spans]
        self._maxlen = max((s.hi - s.lo for s in self.spans), default=0)

    @classmethod
    def build(cls, entities: Sequence, cancel: Cancel = _NEVER):
        spans = []
        for n, e in enumerate(entities):
            if not n & 0x3FF:
                cancel.check()
            spans.extend(entity_spans(e))
        return cls(spans)

    def overlapping(self, lo: int, hi: int) -> List[Span]:
        """Spans that share at least one byte with [lo, hi)."""
        a = bisect.bisect_left(self._los, lo - self._maxlen)
        b = bisect.bisect_left(self._los, hi)
        return [s for s in self.spans[a:b] if s.hi > lo and s.lo < hi]

    def nearest(self, addr: int) -> Optional[Span]:
        """The span whose edge is closest to ``addr`` (for a "not found" hint)."""
        if not self.spans:
            return None
        i = bisect.bisect_left(self._los, addr)
        cand = self.spans[max(0, i - 3):i + 3]
        return min(cand, key=lambda s: min(abs(s.lo - addr),
                                           abs(addr - s.hi + 1)))


# ------------------------------------------------------------- parsers ---
def parse_addresses(text: str, base: int = 16) -> List[Tuple[int, int]]:
    """``0x1A2B3C``, ``$1A2B3C``, ``1A2B3C`` (in ``base``), ``a-b``, ``a..b``,
    ``a+len`` -- several separated by commas or spaces. Returns [lo, hi)."""
    out = []
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\S+?)(?:(\.\.|-|\+)(\S+))?", part)
        if not m:
            raise QueryError("Not an address: %r" % part)
        lo = _addr(m.group(1), base)
        if m.group(2) is None:
            out.append((lo, lo + 1))
        elif m.group(2) == "+":
            n = _addr(m.group(3), base)
            if n <= 0:
                raise QueryError("A length must be at least 1 byte.")
            out.append((lo, lo + n))
        else:
            hi = _addr(m.group(3), base)
            if hi < lo:
                lo, hi = hi, lo
            out.append((lo, hi + 1))
    if not out:
        raise QueryError("Type an address, e.g. 0x1A2B3C or 1A2B3C-1A2BFF.")
    return out


def _addr(tok: str, base: int) -> int:
    t = tok.strip()
    try:
        if t.lower().startswith("0x"):
            return int(t[2:], 16)
        if t.startswith("$"):
            return int(t[1:], 16)
        if t.lower().endswith("h") and base == 16:
            return int(t[:-1], 16)
        return int(t, base)
    except ValueError:
        raise QueryError("Not a valid %s address: %r"
                         % ("hex" if base == 16 else "decimal", tok))


_CMP = re.compile(r"^\s*(>=|<=|!=|==|=|>|<)\s*(.+?)\s*$")


def _num(s: str) -> float:
    s = s.strip()
    try:
        if s.lower().startswith("0x"):
            return float(int(s, 16))
        return float(s)
    except ValueError:
        raise QueryError("Not a number: %r" % s)


def parse_value_query(text: str, tol: float = 0.0) -> Callable[[float], bool]:
    """``12.5`` | ``10..20`` | ``>5`` | ``<=0`` | ``!=0`` | ``nan``."""
    t = text.strip()
    if not t:
        raise QueryError("Type a value, a range (10..20) or a comparison (>5).")
    if t.lower() == "nan":
        return lambda v: v != v
    m = _CMP.match(t)
    if m:
        op, x = m.group(1), _num(m.group(2))
        eps = max(tol, 1e-6)
        return {
            ">": lambda v: v > x, "<": lambda v: v < x,
            ">=": lambda v: v >= x - eps, "<=": lambda v: v <= x + eps,
            "=": lambda v: abs(v - x) <= eps, "==": lambda v: abs(v - x) <= eps,
            "!=": lambda v: abs(v - x) > eps,
        }[op]
    if ".." in t:
        a, _, b = t.partition("..")
        lo, hi = _num(a), _num(b)
        if lo > hi:
            lo, hi = hi, lo
        return lambda v: lo <= v <= hi
    x = _num(t)
    eps = max(tol, 1e-6)
    return lambda v: abs(v - x) <= eps


def parse_byte_pattern(text: str, opts: SearchOptions,
                       default_endian: str = ">") -> Tuple[re.Pattern, int]:
    """A compiled byte regex and the pattern length."""
    kind = opts.bytes_kind
    t = text.strip()
    if not t:
        raise QueryError("Type a byte pattern to look for.")
    en = {"big": ">", "little": "<"}.get(opts.endian, default_endian)
    if kind == "hex":
        toks = re.findall(r"\?\?|\?|[0-9A-Fa-f]{2}|\S+",
                          t.replace("0x", " ").replace(",", " "))
        parts = []
        for tok in toks:
            if tok in ("??", "?"):
                parts.append(b".")
            elif re.fullmatch(r"[0-9A-Fa-f]{2}", tok):
                parts.append(re.escape(bytes([int(tok, 16)])))
            else:
                raise QueryError(
                    "Hex bytes are two digits each (e.g. DE AD ?? EF); "
                    "%r is not." % tok)
        n = len(parts)
        body = b"".join(parts)
    elif kind == "ascii":
        try:
            raw = t.encode("latin-1")
        except UnicodeEncodeError:
            raise QueryError("ASCII search takes characters up to U+00FF.")
        n = len(raw)
        body = re.escape(raw)
    elif kind == "int":
        v = int(_num(t))
        w = opts.width
        if w not in (8, 16, 32, 64):
            raise QueryError("Integer width must be 8, 16, 32 or 64 bits.")
        lo, hi = ((-(1 << (w - 1)), (1 << (w - 1)) - 1) if opts.signed
                  else (0, (1 << w) - 1))
        if not lo <= v <= hi:
            raise QueryError("%d does not fit in %s %d bits (%d..%d)."
                             % (v, "signed" if opts.signed else "unsigned",
                                w, lo, hi))
        raw = v.to_bytes(w // 8, "big" if en == ">" else "little",
                         signed=opts.signed)
        n = len(raw)
        body = re.escape(raw)
    elif kind == "float":
        if opts.width not in (32, 64):
            raise QueryError("Float width must be 32 or 64 bits.")
        x = _num(t)
        try:
            raw = struct.pack(en + ("f" if opts.width == 32 else "d"), x)
        except (struct.error, OverflowError):
            raise QueryError("%r does not fit a %d-bit float." % (t, opts.width))
        n = len(raw)
        body = re.escape(raw)
    else:
        raise QueryError("Unknown pattern type %r." % kind)
    if n == 0:
        raise QueryError("Type a byte pattern to look for.")
    flags = re.DOTALL
    if kind == "ascii" and not opts.case_sensitive:
        flags |= re.IGNORECASE
    return re.compile(b"(?=(" + body + b"))", flags), n


# -------------------------------------------------------------- history --
class History:
    """Most-recent-first list of queries, kept across sessions."""

    def __init__(self, limit: int = 50):
        self.limit = limit
        self.items: List[Tuple[str, str]] = []     # (mode, query)

    def push(self, mode: str, query: str):
        q = query.strip()
        if not q:
            return
        self.items = [i for i in self.items if i != (mode, q)]
        self.items.insert(0, (mode, q))
        del self.items[self.limit:]

    def for_mode(self, mode: str) -> List[str]:
        return [q for m, q in self.items if m == mode]

    def clear(self):
        self.items = []

    def dumps(self) -> str:
        return json.dumps(self.items)

    @classmethod
    def loads(cls, s: str, limit: int = 50) -> "History":
        h = cls(limit)
        try:
            for m, q in json.loads(s or "[]"):
                if m in MODES and isinstance(q, str):
                    h.items.append((m, q))
        except (ValueError, TypeError):
            pass
        del h.items[limit:]
        return h


# --------------------------------------------------------- shipped index --
def _data_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def index_path() -> str:
    """The shipped label cache: EDC16 module data (its labels are the EDC16
    definitions'), wherever that module is installed."""
    from . import datafiles
    return datafiles.path(INDEX_FILE) or os.path.join(_data_dir(), INDEX_FILE)


def current_digests() -> Tuple[str, str]:
    """(definition-data digest, label-code digest) as they are right now.

    The data digest covers the data of the module that owns the index (so
    installing or removing another module does not make it stale), and skips
    the index file itself, which would otherwise have to contain a hash of
    itself."""
    from . import defn, datafiles
    files = [p for n, p in datafiles.listing(datafiles.owner(INDEX_FILE))
             if n != INDEX_FILE]
    return defn._hash_files(sorted(files)), defn.ui_digest()


def load_shipped_titles(path: Optional[str] = None,
                        digests: Optional[Callable[[], Tuple[str, str]]] = None
                        ) -> Optional[Dict[str, str]]:
    """The pre-built label cache, or None when it is missing or stale.

    It records the digest of the definition data and of the code that makes
    labels; if either has changed since it was built it is ignored, so a stale
    index can slow nothing down but can never show a wrong label."""
    p = path or index_path()
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if raw.get("format") != FORMAT:
        return None
    try:
        want = (digests or current_digests)()
    except Exception:
        return None
    if (raw.get("data"), raw.get("ui")) != want:
        return None
    t = raw.get("titles")
    return t if isinstance(t, dict) else None


# --------------------------------------------------------------- engine ---
class SearchEngine:
    """Runs searches over sources and caches what is expensive to build.

    ``title_fn(entity)`` produces the tree's English label (the GUI passes its
    own, so search and tree always agree); ``tier_name(tier)`` the tier's name.
    """

    def __init__(self, title_fn: Callable, tier_name: Callable = str,
                 titles: Optional[Dict[str, str]] = None):
        self.title_fn = title_fn
        self.tier_name = tier_name
        self.titles = titles
        self._corpus: Dict[int, tuple] = {}
        self._addr: Dict[int, tuple] = {}

    # -- per-source caches (keyed by session identity + entity count) ------
    @staticmethod
    def entities_of(session) -> List:
        ents = list(session.entities)
        try:
            ents += list(session.variant_entities())
        except Exception:
            pass
        return ents

    @staticmethod
    def _cached(cache: dict, session, key, make):
        hit = cache.get(id(session))
        if hit and hit[0] == key and hit[1]() is session:
            return hit[2]
        for k in [k for k, v in cache.items() if v[1]() is None]:
            del cache[k]                    # a closed tab's leftovers
        val = make()
        cache[id(session)] = (key, weakref.ref(session), val)
        return val

    def corpus(self, src: Source, cancel: Cancel = _NEVER) -> Corpus:
        ents = self.entities_of(src.session)
        return self._cached(
            self._corpus, src.session, len(ents),
            lambda: Corpus.build(ents, self.title_fn, self.tier_name,
                                 self.titles, cancel))

    def addresses(self, src: Source, cancel: Cancel = _NEVER) -> AddressIndex:
        ents = self.entities_of(src.session)
        return self._cached(self._addr, src.session, len(ents),
                            lambda: AddressIndex.build(ents, cancel))

    def forget(self, src: Source):
        self._corpus.pop(id(src.session), None)
        self._addr.pop(id(src.session), None)

    # -- the one entry point ------------------------------------------------
    def run(self, query: str, opts: SearchOptions, sources: Sequence[Source],
            cancel: Cancel = _NEVER,
            progress: Optional[Callable[[int, int], None]] = None,
            target=None) -> List[Hit]:
        """Search ``sources``. ``target`` is the one parameter a byte search
        looks inside. Raises :class:`QueryError` for an unusable query and
        :class:`Cancelled` if ``cancel`` is set."""
        if opts.mode == "text":
            return self._text(query, opts, sources, cancel, progress)
        if opts.mode == "value":
            return self._value(query, opts, sources, cancel, progress)
        if opts.mode == "address":
            return self._address(query, opts, sources, cancel, progress)
        if opts.mode == "bytes":
            return self._bytes(query, opts, sources, cancel, progress, target)
        raise QueryError("Unknown search mode %r." % opts.mode)

    # -- text ---------------------------------------------------------------
    def _text(self, query, opts, sources, cancel, progress):
        m = _TextMatcher(query, opts)
        hits: List[Hit] = []
        total = len(sources)
        for si, src in enumerate(sources):
            corpus = self.corpus(src, cancel)
            n = len(corpus.docs)
            for i, d in enumerate(corpus.docs):
                if not i & 0x1FF:
                    cancel.check()
                    if progress:
                        progress(si * n + i, total * n)
                if opts.tier and d.ent.tier != opts.tier:
                    continue
                r = m.match(d)
                if r is not None:
                    hits.append(Hit(d.ent, src, r[0], r[1], r[2]))
        hits.sort(key=lambda h: (-h.score, len(h.entity.name or ""),
                                 h.entity.name or ""))
        return hits[:opts.max_results]

    # -- address ------------------------------------------------------------
    def _address(self, query, opts, sources, cancel, progress):
        ranges = parse_addresses(query, opts.base)
        hits: List[Hit] = []
        for src in sources:
            idx = self.addresses(src, cancel)
            for lo, hi in ranges:
                cancel.check()
                for sp in idx.overlapping(lo, hi):
                    if opts.tier and sp.ent.tier != opts.tier:
                        continue
                    a, b = max(lo, sp.lo), min(hi, sp.hi)
                    first = sp.cell_at(a)
                    # cells touched (a multi-byte cell is hit by any byte)
                    n_first = (a - sp.lo) // sp.step
                    n_last = (b - 1 - sp.lo) // sp.step
                    cells = tuple(sp.cell_at(sp.lo + k * sp.step)
                                  for k in range(n_first,
                                                 min(n_last, n_first + MAX_CELLS - 1) + 1))
                    off = a - sp.lo
                    where = ("%s +0x%X" % (sp.label(), off)
                             if sp.kind == "z" and sp.ent.rows * sp.ent.cols == 1
                             else "%s %s" % (sp.label(), _cell_name(first)))
                    detail = "0x%X is in %s, byte %d of %d (0x%X..0x%X)" % (
                        a, where, off, sp.hi - sp.lo, sp.lo, sp.hi - 1)
                    # the parameter's own start ranks above a hit mid-way
                    score = 100.0 - (0 if a == sp.lo else 1.0)
                    hits.append(Hit(sp.ent, src, score, where, detail, a,
                                    cells, n_last - n_first + 1))
        hits.sort(key=lambda h: (h.addr or 0, -h.score))
        return hits[:opts.max_results]

    def nearest_hint(self, query: str, opts: SearchOptions,
                     sources: Sequence[Source]) -> str:
        """For an address search with no result: where the nearest parameter is."""
        try:
            lo = parse_addresses(query, opts.base)[0][0]
        except QueryError:
            return ""
        best = None
        for src in sources:
            sp = self.addresses(src).nearest(lo)
            if sp is None:
                continue
            d = min(abs(sp.lo - lo), abs(lo - (sp.hi - 1)))
            if best is None or d < best[0]:
                best = (d, sp)
        if best is None:
            return ""
        d, sp = best
        return ("Nothing covers 0x%X. The nearest is %s (%s, 0x%X..0x%X), "
                "%d byte%s away." % (lo, sp.ent.name or sp.ent.title,
                                     sp.label(), sp.lo, sp.hi - 1, d,
                                     "" if d == 1 else "s"))

    # -- value --------------------------------------------------------------
    def _value(self, query, opts, sources, cancel, progress):
        pred = parse_value_query(query, opts.tolerance)
        hits: List[Hit] = []
        for si, src in enumerate(sources):
            ents = self.entities_of(src.session)
            n = len(ents)
            for i, e in enumerate(ents):
                if not i & 0x3F:
                    cancel.check()
                    if progress:
                        progress(si * n + i, len(sources) * n)
                if opts.tier and e.tier != opts.tier:
                    continue
                if getattr(e, "text_kind", ""):
                    continue                # characters, not quantities
                cells = _matching_cells(e, pred, opts)
                if cells:
                    hits.append(_value_hit(e, src, cells))
        hits.sort(key=lambda h: (-h.n_cells, h.entity.name or ""))
        return hits[:opts.max_results]

    # -- bytes --------------------------------------------------------------
    def _bytes(self, query, opts, sources, cancel, progress, target):
        if target is None:
            raise QueryError("Pick a parameter to search inside (open one in "
                             "the table first). Searching the whole image is "
                             "what the hex editor is for.")
        hits: List[Hit] = []
        for src in sources:
            e = _twin(src.session, target)
            if e is None:
                continue
            bf = e.binfile
            data = bf.data
            pat, n = parse_byte_pattern(query, opts,
                                        getattr(bf, "endian", ">"))
            for sp in entity_spans(e, axes=opts.include_axes):
                cancel.check()
                seg = bytes(data[sp.lo:sp.hi])
                for m in pat.finditer(seg):
                    a = sp.lo + m.start()
                    last = min(sp.hi - 1, a + n - 1)
                    k0 = (a - sp.lo) // sp.step
                    k1 = (last - sp.lo) // sp.step
                    cells = tuple(sp.cell_at(sp.lo + k * sp.step)
                                  for k in range(k0, min(k1, k0 + 63) + 1))
                    where = "%s %s" % (sp.label(), _cell_name(cells[0]))
                    hits.append(Hit(e, src, 100.0, where,
                                    "%d byte%s at 0x%X in %s" % (
                                        n, "" if n == 1 else "s", a, where),
                                    a, cells, k1 - k0 + 1))
                    if len(hits) >= opts.max_results:
                        break
        hits.sort(key=lambda h: h.addr or 0)
        return hits


def _twin(session, target):
    """``target`` as it exists in ``session`` (same object, or the entity of
    the same name), so one pattern can be run against a comparison too."""
    ents = SearchEngine.entities_of(session)
    for e in ents:
        if e is target:
            return e
    key = (target.name, getattr(target, "label", ""), target.kind)
    for e in ents:
        if (e.name, getattr(e, "label", ""), e.kind) == key:
            return e
    return None


def _cell_name(c: tuple) -> str:
    k, a, b = c
    if k == "z":
        return "[%d,%d]" % (a, b)
    return "[%d]" % a


def _matching_cells(e, pred, opts) -> List[tuple]:
    out = []
    try:
        if opts.raw_values:
            for r in range(e.rows):
                for c in range(e.cols):
                    v = e.raw_cell(r, c)
                    if pred(v):
                        out.append(("z", r, c, v))
        else:
            for r, row in enumerate(e.values()):
                for c, v in enumerate(row):
                    if pred(v):
                        out.append(("z", r, c, v))
        if opts.include_axes:
            for kind, ax in (("x", e.x), ("y", e.y)):
                if ax is None or ax.addr is None:
                    continue
                if opts.raw_values:
                    if ax.is_float:
                        continue
                    vals = e.binfile.read_many(ax.addr, ax.count, ax.bits,
                                               False)
                else:
                    vals = e.axis_values(ax)
                for i, v in enumerate(vals):
                    if pred(v):
                        out.append((kind, i, 0, v))
    except Exception:
        return []                # an unreadable entity is not a match
    return out


def _value_hit(e, src, cells) -> Hit:
    shown = ", ".join("%s%s=%s" % (
        {"z": "", "x": "X", "y": "Y"}[k],
        _cell_name((k, a, b)), _fmt(v)) for k, a, b, v in cells[:3])
    more = "" if len(cells) <= 3 else " … +%d more" % (len(cells) - 3)
    return Hit(e, src, float(len(cells)), "value",
               "%d match%s: %s%s" % (len(cells),
                                     "" if len(cells) == 1 else "es",
                                     shown, more),
               None, tuple((k, a, b) for k, a, b, _ in cells[:MAX_CELLS]),
               len(cells))


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        return ("%.6g" % v)
    return str(v)
