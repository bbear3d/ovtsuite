# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saved assistant chats (ai/chats.py): the conversation round-trips with
its tool calls and the services' replay data, files are private and written
whole, the size cap deletes the chats used longest ago, and a reopened chat
a service refuses carries on without the replay data. No Qt."""

import os
import sys

import pytest

from ovtsuite.ai import chats as CH
from ovtsuite.ai.providers.base import Call


def _conversation():
    return [
        {"role": "user", "text": "[What I am looking at: tab t1]\n\nWhat changed?"},
        {"role": "assistant", "text": "", "calls": [Call("c1", "diff", {"handle": "t1"})],
         "raw": [{"type": "thinking", "thinking": "hm", "signature": "sig=="},
                 {"type": "tool_use", "id": "c1", "name": "diff", "input": {"handle": "t1"}}],
         "raw_kind": "anthropic"},
        {"role": "tool", "results": [{"id": "c1", "name": "diff", "content": "x" * 50000,
                                      "is_error": False}]},
        {"role": "assistant", "text": "Boost and smoke changed.", "calls": [],
         "raw": [{"type": "text", "text": "Boost and smoke changed."}],
         "raw_kind": "anthropic"},
        {"role": "user", "text": "And the timing?"},
        {"role": "assistant", "text": "", "calls": [Call("c2", "get_map", {"map": "Z"})],
         "raw": None, "raw_kind": "openai"},
        {"role": "tool", "results": [{"id": "c2", "name": "get_map", "content": "y" * 50000,
                                      "is_error": True}]},
    ]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_AI_DIR", str(tmp_path))
    return CH.store()


def test_round_trip_keeps_calls_and_replay_data(store):
    msgs = _conversation()
    chat = [("you", "What changed?"), ("ai", "Boost and smoke changed."),
            ("map", ("t1", "Z", "Timing")), ("proposal", ("smoke +1", "p1")),
            ("plan", ("Stage 1", 3, "a", "c"))]
    rec = CH.record(msgs, chat, cid="", title=CH.title_of("What changed?"),
                    service="anthropic", model="claude-x", project_uid="u1",
                    project_name="Golf", tokens=1234)
    meta = store.save(rec)
    back = store.load(meta["id"])
    m2 = CH.messages_from_json(back["messages"])
    assert [m["role"] for m in m2] == [m["role"] for m in msgs]
    assert m2[1]["calls"] == [Call("c1", "diff", {"handle": "t1"})]   # Call objects again
    assert m2[1]["raw"] == msgs[1]["raw"] and m2[1]["raw_kind"] == "anthropic"
    # an earlier question's long tool result is cut; the last question's is whole
    assert len(m2[2]["results"][0]["content"]) < 20000
    assert m2[6]["results"][0]["content"] == "y" * 50000
    assert m2[6]["results"][0]["is_error"] is True
    assert CH.chat_from_json(back["chat"]) == chat
    assert back["tokens"] == 1234 and back["project_uid"] == "u1"
    # nothing is changed in the live conversation by saving it
    assert msgs[2]["results"][0]["content"] == "x" * 50000
    m = store.list()[0]
    assert m["title"] == "What changed?" and m["messages"] == len(msgs)
    assert m["model"] == "claude-x" and m["size"] > 0
    assert store.list(project_uid="u1") and not store.list(project_uid="other")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_files_are_private_and_written_whole(store):
    meta = store.save(CH.record([], [("you", "hi")], title="hi"))
    d = CH.chats_dir()
    assert os.stat(d).st_mode & 0o077 == 0
    for fn in os.listdir(d):
        assert not fn.endswith(".tmp")
        assert os.stat(os.path.join(d, fn)).st_mode & 0o077 == 0, fn
    assert os.path.isfile(os.path.join(d, meta["id"] + CH.SUFFIX))


def test_rename_delete_clear_and_lost_index(store):
    a = store.save(CH.record([], [("you", "one")], title="one"))
    b = store.save(CH.record([], [("you", "two")], title="two"))
    store.rename(a["id"], "  First  chat ")
    assert {m["title"] for m in store.list()} == {"First chat", "two"}
    store.delete(b["id"])
    assert [m["id"] for m in store.list()] == [a["id"]]
    # the index is rebuilt from the chats when it is lost
    os.remove(os.path.join(CH.chats_dir(), CH.INDEX))
    fresh = CH.ChatStore()
    assert [m["title"] for m in fresh.list()] == ["First chat"]
    c = fresh.save(CH.record([], [("you", "three")], title="three"))
    assert fresh.clear(keep=[c["id"]]) == 1
    assert [m["id"] for m in fresh.list()] == [c["id"]]
    assert fresh.clear() == 1 and fresh.list() == []


def test_cap_deletes_the_chats_used_longest_ago(store):
    big = [{"role": "user", "text": os.urandom(40000).hex()}]       # does not compress
    ids = []
    for i in range(4):
        rec = CH.record(big, [("you", "q%d" % i)], title="q%d" % i)
        rec["updated"] = "2026-01-0%dT00:00:00" % (i + 1)
        ids.append(store.save(rec)["id"])
    one = store.list()[0]["size"]
    # the oldest is the open chat: kept; the next oldest go
    removed = store.enforce_cap(2 * one + 10, keep=[ids[0]])
    assert removed == [ids[1], ids[2]]
    assert sorted(m["id"] for m in store.list()) == sorted([ids[0], ids[3]])
    assert store.total_size() <= 2 * one + 10
    assert store.enforce_cap(0) == []                  # no cap, nothing deleted


def test_save_async_runs_in_order_and_caps(store):
    got = []
    big = [{"role": "user", "text": os.urandom(30000).hex()}]
    first = store.save_async(CH.record(big, [("you", "a")], title="a")).result(10)
    store.save_async(CH.record(big, [("you", "b")], title="b"),
                     cap_bytes=first["size"] + 10,
                     done=lambda m, removed, err: got.append((m["title"], removed, err)))
    store.flush()
    assert got == [("b", [first["id"]], None)]
    assert [m["title"] for m in store.list()] == ["b"]


def test_a_refused_reopened_chat_continues_without_replay_data(store):
    """After an app update a saved thinking signature (or a service's other
    replay data) may be refused: the chat goes on from its text."""
    from ovtsuite.ai.assistant import Assistant
    from ovtsuite.ai.providers import BAD, ProviderError
    from ovtsuite.ai.providers.base import Turn

    class P:
        class spec:
            id, label, local = "anthropic", "Claude", False
        kind, model, label = "anthropic", "m", "Claude"

        def __init__(self):
            self.seen = []

        def complete(self, system, messages, tools, max_tokens=0, on_text=None):
            self.seen.append([dict(m) for m in messages])
            if any(m.get("raw") for m in messages if m["role"] == "assistant"):
                raise ProviderError(BAD, "invalid signature", 400)
            return Turn(text="Fine.")

    p = P()
    a = Assistant(None, cfg={"stream": False}, provider=p)
    a.messages[:] = CH.messages_from_json(CH.messages_to_json(_conversation()))
    a.restored = True
    notes = []
    a.on_event = lambda ev: notes.append(ev)
    r = a.ask("Anything else?")
    assert r.text == "Fine." and len(p.seen) == 2
    assert not any(m.get("raw") for m in p.seen[1] if m["role"] == "assistant")
    assert any(ev.kind == "notice" for ev in notes)
    assert a.restored is False
