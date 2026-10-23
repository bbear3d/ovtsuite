# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Converter translation: glossary, built-in, memory, online (faked)."""

import os

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import translate as T
from ovtsuite.definitions.model import Definition, Storage, TableDef


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


def _defn(*texts, symbol=""):
    d = Definition(type="xdf")
    for i, t in enumerate(texts):
        d.tables.append(TableDef(name=t, symbol=symbol, kind="constant",
                                 z=Storage(addr=i * 2, bits=16), description=t))
    return d


def test_glossary_wins_and_identifiers_stay():
    d = _defn("Fahrerwunsch", symbol="")
    rows = T.plan(d, {"backend": "builtin"})
    assert rows[0]["result"] == "Driver's Wish" and rows[0]["by"] == "glossary"
    d = _defn("mrwFAHRWU", symbol="mrwFAHRWU")
    assert all(r["field"] != "name" for r in T.plan(d, {"backend": "builtin"}))


def test_bilingual_and_originals_kept():
    d = _defn("Fahrerwunsch")
    T.apply(d, {"backend": "builtin", "mode": "bilingual"})
    t = d.tables[0]
    assert t.name == "Fahrerwunsch / Driver's Wish"
    assert t.originals["name"] == "Fahrerwunsch"


class _FakeHttp:
    calls = 0

    def request(self, method, url, headers, body, log_url=None):
        _FakeHttp.calls += 1
        return {"translatedText": ["EN " + q for q in body["q"]]}


def test_online_then_memory(monkeypatch):
    monkeypatch.setattr(T, "_http", lambda: _FakeHttp())
    d = _defn("Bonjour le monde")
    opts = {"backend": "online", "provider": "libretranslate", "source": "fr",
            "target": "en"}
    rows = T.plan(d, opts)
    assert rows[0]["result"] == "EN Bonjour le monde"
    n = _FakeHttp.calls
    rows = T.plan(_defn("Bonjour le monde"), opts)
    assert rows[0]["by"] == "memory" and _FakeHttp.calls == n


def test_online_failure_keeps_builtin(monkeypatch):
    def boom(*a, **k):
        raise T.OnlineError("offline")
    monkeypatch.setattr(T, "online", boom)
    warn = []
    rows = T.plan(_defn("Bonjour le monde"),
                  {"backend": "online", "source": "fr", "target": "en"}, warn)
    assert warn and rows[0]["result"] == "Bonjour le monde"     # left as it was


def test_default_provider_is_a_setting():
    assert T.settings()["provider"] == "libretranslate"
    T.save_settings(provider="deepl")
    assert T.settings()["provider"] == "deepl"
