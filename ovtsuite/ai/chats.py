# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saved assistant chats, per user: ``<ai dir>/chats/``.

One gzip'd JSON file per chat (``<id>.json.gz``) holds the conversation as
the assistant keeps it (:attr:`.assistant.Assistant.messages`, provider-
neutral, with each service's own replay data ``raw``) and the transcript the
panel shows, so a chat reopens as it was and can be carried on. A small
``index.json`` lists them (title, when, service and model, the project it
was about, how big) so the history menu never opens the chats themselves.

Files are this user's only (0600 in a 0700 folder on POSIX; on Windows the
per-user AppData folder) and written whole or not at all (a temporary file
then ``os.replace``). API keys are never in a chat: they are not part of the
conversation. No Qt: the panel saves on a worker thread.
"""

from __future__ import annotations

import gzip
import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional

from . import config as _cfg

VERSION = 1
INDEX = "index.json"
SUFFIX = ".json.gz"
#: tool results of earlier questions longer than this are cut when saved:
#: the answers built on them are kept whole, and a reopened chat need not
#: carry a whole diff it was given an hour ago
OLD_RESULT_MAX = 16000
#: the default for the "Maximum saved chat history" option (MB)
DEFAULT_CAP_MB = 50


def chats_dir() -> str:
    d = os.path.join(_cfg.ai_dir(), "chats")
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def new_id() -> str:
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def _now() -> str:
    """Local time to the millisecond: it orders the chats."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + ".%03d" % (t % 1 * 1000)


def _valid(cid: str) -> bool:
    return bool(cid) and all(ch.isalnum() or ch in "-_" for ch in cid)


# ------------------------------------------------------------------ form
def messages_to_json(messages: Iterable[dict], *, compact: bool = True) -> List[dict]:
    """The assistant's conversation as JSON: each tool call as a dict; the
    replay data (``raw``) kept as it came from the service. With
    ``compact``, long tool results of questions before the last are cut
    (:data:`OLD_RESULT_MAX`)."""
    msgs = list(messages)
    last_q = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    out = []
    for i, m in enumerate(msgs):
        d = dict(m)
        if d.get("role") == "assistant":
            d["calls"] = [{"id": c.id, "name": c.name, "args": c.args}
                          if not isinstance(c, dict) else dict(c)
                          for c in d.get("calls") or ()]
        elif d.get("role") == "tool":
            res = []
            for r in d.get("results") or ():
                r = dict(r)
                c = r.get("content")
                if compact and i < last_q and isinstance(c, str) and len(c) > OLD_RESULT_MAX:
                    r["content"] = c[:OLD_RESULT_MAX] + "...(cut when the chat was saved)"
                res.append(r)
            d["results"] = res
        out.append(d)
    return out


def messages_from_json(data: Iterable[dict]) -> List[dict]:
    from .providers.base import Call
    out = []
    for m in data or ():
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "tool"):
            continue
        d = dict(m)
        if d["role"] == "assistant":
            d["calls"] = [Call(str(c.get("id", "")), str(c.get("name", "")),
                               dict(c.get("args") or {}))
                          for c in d.get("calls") or () if isinstance(c, dict)]
            d.setdefault("text", "")
        elif d["role"] == "tool":
            d["results"] = [dict(r) for r in d.get("results") or () if isinstance(r, dict)]
        else:
            d["text"] = str(d.get("text", ""))
        out.append(d)
    return out


#: transcript entries whose payload is a tuple in the panel
_TUPLES = ("map", "proposal", "plan")


def chat_to_json(chat) -> List[list]:
    return [[k, list(v) if isinstance(v, tuple) else v] for k, v in chat]


def chat_from_json(data) -> List[tuple]:
    out = []
    for item in data or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        k, v = item
        out.append((str(k), tuple(v) if k in _TUPLES and isinstance(v, list) else v))
    return out


def title_of(question: str, n: int = 80) -> str:
    t = " ".join(str(question or "").split())
    return (t if len(t) <= n else t[:n - 1] + "…") or "Chat"


# ------------------------------------------------------------------ store
class ChatStore:
    """The saved chats. Thread-safe; :meth:`save_async` writes on a worker
    thread of its own (in order)."""

    def __init__(self, root: Optional[str] = None):
        self._root = root
        self._lock = threading.RLock()
        self._index: Optional[Dict[str, dict]] = None
        self._pool: Optional[ThreadPoolExecutor] = None

    @property
    def root(self) -> str:
        if self._root is None:
            return chats_dir()
        os.makedirs(self._root, exist_ok=True)
        return self._root

    def _path(self, cid: str) -> str:
        if not _valid(cid):
            raise ValueError("bad chat id %r" % cid)
        return os.path.join(self.root, cid + SUFFIX)

    # -- files ------------------------------------------------------------------
    @staticmethod
    def _write(path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def _load_index(self) -> Dict[str, dict]:
        if self._index is not None:
            return self._index
        idx = {}
        try:
            with open(os.path.join(self.root, INDEX), "r", encoding="utf-8") as fh:
                d = json.load(fh)
            for m in d.get("chats", []):
                if isinstance(m, dict) and _valid(m.get("id", "")):
                    idx[m["id"]] = m
        except (OSError, ValueError, AttributeError):
            idx = self._rebuild()
        # chats deleted behind our back drop out
        idx = {k: v for k, v in idx.items() if os.path.isfile(self._path(k))}
        self._index = idx
        return idx

    def _rebuild(self) -> Dict[str, dict]:
        """The index from the chats themselves (it was lost or broken)."""
        idx = {}
        for fn in os.listdir(self.root):
            if not fn.endswith(SUFFIX):
                continue
            try:
                rec = self._read(os.path.join(self.root, fn))
                m = self._meta(rec, os.path.getsize(os.path.join(self.root, fn)))
                idx[m["id"]] = m
            except Exception:
                continue
        return idx

    def _save_index(self) -> None:
        chats = sorted(self._load_index().values(), key=lambda m: m.get("updated", ""))
        self._write(os.path.join(self.root, INDEX),
                    json.dumps({"version": VERSION, "chats": chats}, indent=1).encode("utf-8"))

    @staticmethod
    def _read(path: str) -> dict:
        with gzip.open(path, "rb") as fh:
            rec = json.loads(fh.read().decode("utf-8"))
        if not isinstance(rec, dict):
            raise ValueError("not a saved chat")
        return rec

    @staticmethod
    def _meta(rec: dict, size: int) -> dict:
        keys = ("id", "title", "created", "updated", "service", "model",
                "project_uid", "project_name", "tokens")
        m = {k: rec.get(k) for k in keys if rec.get(k) not in (None, "")}
        m["messages"] = len(rec.get("messages") or ())
        m["size"] = int(size)
        return m

    # -- public -----------------------------------------------------------------
    def list(self, project_uid: Optional[str] = None) -> List[dict]:
        """Saved chats, most recently used first (only those about the
        project ``project_uid`` when given)."""
        with self._lock:
            ms = [dict(m) for m in self._load_index().values()]
        if project_uid:
            ms = [m for m in ms if m.get("project_uid") == project_uid]
        ms.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return ms

    def last(self) -> Optional[dict]:
        ms = self.list()
        return ms[0] if ms else None

    def total_size(self) -> int:
        with self._lock:
            return sum(int(m.get("size") or 0) for m in self._load_index().values())

    def load(self, cid: str) -> dict:
        rec = self._read(self._path(cid))
        rec["id"] = cid
        return rec

    def save(self, rec: dict) -> dict:
        """Write one chat (``rec``: id, title, created, service, model,
        project_uid, tokens, messages -- already JSON, see
        :func:`messages_to_json` -- and chat). Returns its index entry."""
        rec = dict(rec)
        cid = rec.get("id") or new_id()
        rec["id"] = cid
        rec["version"] = VERSION
        rec.setdefault("created", _now())
        rec["updated"] = rec.get("updated") or _now()
        data = gzip.compress(json.dumps(rec, ensure_ascii=False, separators=(",", ":"),
                                        default=str).encode("utf-8"), 6, mtime=0)
        path = self._path(cid)
        with self._lock:
            self._write(path, data)
            m = self._meta(rec, len(data))
            self._load_index()[cid] = m
            self._save_index()
        return dict(m)

    def save_async(self, rec: dict, *, cap_bytes: int = 0, keep: Iterable[str] = (),
                   done=None):
        """:meth:`save` (then :meth:`enforce_cap`) on the store's worker
        thread; ``done(meta, removed, error)`` is called there."""
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="ovt-ai-chats")
            pool = self._pool

        def run():
            try:
                m = self.save(rec)
                removed = self.enforce_cap(cap_bytes, keep=list(keep) + [m["id"]]) \
                    if cap_bytes else []
                err = None
            except Exception as ex:
                m, removed, err = None, [], ex
            if done is not None:
                try:
                    done(m, removed, err)
                except Exception:
                    pass
            return m
        return pool.submit(run)

    def flush(self, timeout: float = 10.0) -> None:
        """Wait for saves already handed to :meth:`save_async`."""
        pool = self._pool
        if pool is not None:
            pool.submit(lambda: None).result(timeout)

    def rename(self, cid: str, title: str) -> None:
        title = title_of(title, 200)
        with self._lock:
            rec = self.load(cid)
            rec["title"] = title
            self.save(rec)                  # "updated" kept: renaming is not using

    def delete(self, cid: str) -> None:
        with self._lock:
            try:
                os.remove(self._path(cid))
            except FileNotFoundError:
                pass
            self._load_index().pop(cid, None)
            self._save_index()

    def clear(self, keep: Iterable[str] = ()) -> int:
        """Delete every saved chat (but ``keep``); returns how many."""
        keep = set(keep)
        n = 0
        with self._lock:
            for cid in list(self._load_index()):
                if cid in keep:
                    continue
                try:
                    os.remove(self._path(cid))
                except FileNotFoundError:
                    pass
                self._load_index().pop(cid, None)
                n += 1
            # strays the index did not know about
            for fn in os.listdir(self.root):
                if fn.endswith(SUFFIX) and fn[:-len(SUFFIX)] not in keep \
                        and fn[:-len(SUFFIX)] not in self._load_index():
                    try:
                        os.remove(os.path.join(self.root, fn))
                    except OSError:
                        pass
            self._save_index()
        return n

    def enforce_cap(self, cap_bytes: int, keep: Iterable[str] = ()) -> List[str]:
        """Delete the least recently used chats (never those in ``keep``)
        until the saved chats take no more than ``cap_bytes``."""
        if cap_bytes <= 0:
            return []
        keep = set(keep)
        removed = []
        with self._lock:
            idx = self._load_index()
            total = sum(int(m.get("size") or 0) for m in idx.values())
            for m in sorted(idx.values(), key=lambda m: m.get("updated", "")):
                if total <= cap_bytes:
                    break
                if m["id"] in keep:
                    continue
                try:
                    os.remove(self._path(m["id"]))
                except FileNotFoundError:
                    pass
                idx.pop(m["id"], None)
                total -= int(m.get("size") or 0)
                removed.append(m["id"])
            if removed:
                self._save_index()
        return removed


def record(assistant_messages, chat, *, cid: str = "", title: str = "", created: str = "",
           service: str = "", model: str = "", project_uid: str = "",
           project_name: str = "", tokens: int = 0) -> dict:
    """What :meth:`ChatStore.save` writes, from the live conversation. Call
    it where the conversation lives (the GUI thread): it copies the lists
    and messages it keeps (cheap -- their contents are never changed once
    made), so a question asked while it is written changes nothing saved."""
    msgs = messages_to_json([dict(m) for m in assistant_messages])
    return {"id": cid, "title": title or "Chat", "created": created or _now(),
            "updated": _now(), "service": service, "model": model,
            "project_uid": project_uid, "project_name": project_name,
            "tokens": int(tokens or 0), "messages": msgs,
            "chat": chat_to_json(chat)}


_STORE: Optional[ChatStore] = None
_STORE_DIR: Optional[str] = None


def store() -> ChatStore:
    """The store for the current AI folder (tests point ``OVT_AI_DIR``
    elsewhere: a new store follows)."""
    global _STORE, _STORE_DIR
    d = _cfg.ai_dir()
    if _STORE is None or _STORE_DIR != d:
        _STORE, _STORE_DIR = ChatStore(), d
    return _STORE
