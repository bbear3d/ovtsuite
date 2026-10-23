# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Translating a definition's display text -- never its identifiers.

What is translated: table names that are display text (an XDF or WinOLS
title; an A2L / DAMOS name is an identifier and stays), descriptions,
category names, axis names, units and axis point texts. Per text, in order:

1. the **glossary** (the user's terms over the shipped ones) -- always wins;
2. **translations the file carries** (WinOLS multilingual strings, when the
   reader keeps them);
3. the **built-in library** (:mod:`ovtsuite.signal_names`, German to
   English, offline);
4. the **translation memory** -- every result kept, so a re-run is free and
   works offline;
5. an **online translator**, only when asked: DeepL, Google Cloud
   Translation, Microsoft Translator, LibreTranslate, or the AI service set
   up for OVT Assistant. Bring-your-own key; only the strings are sent,
   never binary data. The default provider is chosen in Options and a
   conversion can name another.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

#: providers, in the order the Options list shows them
PROVIDERS = {
    "libretranslate": "LibreTranslate (free, self-hostable)",
    "deepl": "DeepL",
    "google": "Google Cloud Translation",
    "microsoft": "Microsoft Translator",
    "ai": "The AI service set up for OVT Assistant",
}
#: providers that need no key
KEYLESS = ("libretranslate",)
DEFAULT_LIBRE = "https://libretranslate.com"
CONFIG = "converter.json"
GLOSSARY = "converter_glossary.json"
MEMORY = "converter_memory.json"

#: shipped glossary (German -> English): domain terms with one settled
#: English form, matched as whole texts or whole words
GLOSSARY_DE_EN = {
    "Fahrerwunsch": "Driver's Wish",
    "Rauchbegrenzung": "Smoke Limiter",
    "Rauchkennfeld": "Smoke map",
    "Ladedruck": "boost pressure",
    "Ladedrucksollwert": "Target Boost",
    "Ladedruckbegrenzung": "boost pressure limit",
    "Drehmomentbegrenzung": "torque limiter",
    "Momentenbegrenzung": "torque limiter",
    "Einspritzbeginn": "start of injection",
    "Spritzbeginn": "start of injection",
    "Einspritzmenge": "injection quantity",
    "Einspritzdauer": "injection duration",
    "Kennfeld": "map",
    "Kennlinie": "curve",
    "Festwert": "constant",
    "Abgasrückführung": "exhaust gas recirculation",
    "Abgasrueckfuehrung": "exhaust gas recirculation",
    "Drehzahl": "engine speed",
    "Kühlmitteltemperatur": "coolant temperature",
    "Kuehlmitteltemperatur": "coolant temperature",
    "Luftmasse": "air mass",
    "Leerlauf": "idle",
    "Mengenbegrenzung": "quantity limiter",
    "Vollast": "full load",
    "Volllast": "full load",
    "Teillast": "part load",
}

_lock = threading.Lock()


# ------------------------------------------------------------------ storage
def config_dir() -> str:
    override = os.environ.get("OVT_CONVERTER_DIR")
    if override:
        return override
    from ..recent import _config_dir
    return os.path.join(_config_dir(), "converter")


def _load(name: str, default):
    try:
        with open(os.path.join(config_dir(), name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(name: str, data) -> None:
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(d, name))


def settings() -> dict:
    """``{"provider": default online translator, "libretranslate_url": ...}``."""
    cfg = {"provider": "libretranslate", "libretranslate_url": DEFAULT_LIBRE}
    cfg.update(_load(CONFIG, {}))
    return cfg


def save_settings(**changes) -> dict:
    cfg = settings()
    cfg.update(changes)
    _save(CONFIG, cfg)
    return cfg


def glossary(source: str = "de", target: str = "en") -> Dict[str, str]:
    """The glossary for a language pair: the shipped terms (German to
    English) under the user's own."""
    out = dict(GLOSSARY_DE_EN) if (source in ("de", "auto") and target == "en") else {}
    user = _load(GLOSSARY, {})
    out.update(user.get("%s>%s" % (source, target), {}))
    if source == "auto":
        out.update(user.get("de>%s" % target, {}))
    return out


def save_glossary(terms: Dict[str, str], source: str = "de", target: str = "en") -> None:
    user = _load(GLOSSARY, {})
    user["%s>%s" % (source, target)] = dict(terms)
    _save(GLOSSARY, user)


def memory() -> Dict[str, Dict[str, str]]:
    return _load(MEMORY, {})


def remember(pair: str, results: Dict[str, str]) -> None:
    with _lock:
        m = memory()
        m.setdefault(pair, {}).update(results)
        _save(MEMORY, m)


# ------------------------------------------------------------------ backends
def _glossary_hit(text: str, g: Dict[str, str]) -> Optional[str]:
    if text in g:
        return g[text]
    if not g:
        return None
    out, hit = text, False
    for k in sorted(g, key=len, reverse=True):
        pat = r"(?<![\w])%s(?![\w])" % re.escape(k)
        if re.search(pat, out):
            out = re.sub(pat, g[k], out)
            hit = True
    return out if hit else None


def builtin(text: str, kind: str = "text") -> Optional[str]:
    """German to English from the built-in library, or None when it has
    nothing to say (an English text, or no German word it knows)."""
    from ..signal_names import _german_words, english_units, phrase_english, translate_german
    if not text or not text.strip():
        return None
    if kind == "units":
        en = english_units(text)
        return en if en != text else None
    if kind == "category":
        from ..xdf_session import category_text
        en = category_text(text)
        return en if en != text else None
    en = phrase_english(text)
    if en:
        return en
    if not _german_words(text):
        return None
    en = translate_german(text)
    return en if en and en != text else None


class OnlineError(RuntimeError):
    pass


def _http():
    from ..ai.providers.base import HttpClient
    return HttpClient(timeout=60)


def _key(provider: str) -> str:
    from ..ai import keystore
    return keystore.get_key("translate-" + provider) or ""


def set_key(provider: str, key: str) -> None:
    from ..ai import keystore
    keystore.set_key("translate-" + provider, key)


def online(texts: List[str], provider: str, source: str, target: str,
           http=None) -> List[str]:
    """``texts`` translated by ``provider`` (raises :class:`OnlineError`)."""
    if not texts:
        return []
    http = http or _http()
    key = _key(provider) if provider not in KEYLESS else ""
    if provider not in KEYLESS and provider != "ai" and not key:
        raise OnlineError("No key set for %s (Options > Converter)." % PROVIDERS.get(provider, provider))
    src = None if source in ("", "auto") else source
    out: List[str] = []
    batch = 50
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        try:
            out.extend(_one(provider, chunk, src, target, key, http))
        except OnlineError:
            raise
        except Exception as exc:
            raise OnlineError("%s: %s" % (PROVIDERS.get(provider, provider), exc))
    if len(out) != len(texts):
        raise OnlineError("%s returned %d texts for %d" % (provider, len(out), len(texts)))
    return out


def _one(provider, chunk, src, target, key, http) -> List[str]:
    if provider == "deepl":
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        body = {"text": chunk, "target_lang": target.upper()}
        if src:
            body["source_lang"] = src.upper()
        r = http.request("POST", "https://%s/v2/translate" % host,
                         {"Authorization": "DeepL-Auth-Key " + key}, body)
        return [t["text"] for t in _json(r)["translations"]]
    if provider == "google":
        body = {"q": chunk, "target": target, "format": "text"}
        if src:
            body["source"] = src
        r = http.request("POST", "https://translation.googleapis.com/language/"
                         "translate/v2?key=" + quote(key), {}, body,
                         log_url="https://translation.googleapis.com/language/translate/v2")
        return [t["translatedText"] for t in _json(r)["data"]["translations"]]
    if provider == "microsoft":
        url = ("https://api.cognitive.microsofttranslator.com/translate?"
               "api-version=3.0&to=" + quote(target) + ("&from=" + quote(src) if src else ""))
        region = settings().get("microsoft_region", "")
        h = {"Ocp-Apim-Subscription-Key": key}
        if region:
            h["Ocp-Apim-Subscription-Region"] = region
        r = http.request("POST", url, h, [{"Text": t} for t in chunk])
        return [t["translations"][0]["text"] for t in _json(r)]
    if provider == "libretranslate":
        url = settings().get("libretranslate_url", DEFAULT_LIBRE).rstrip("/")
        body = {"q": chunk, "source": src or "auto", "target": target, "format": "text"}
        k = _key("libretranslate")
        if k:
            body["api_key"] = k
        r = _json(http.request("POST", url + "/translate", {}, body))
        tt = r["translatedText"]
        return tt if isinstance(tt, list) else [tt]
    if provider == "ai":
        return _ai(chunk, src, target)
    raise OnlineError("Unknown translator %r." % provider)


def _json(r):
    if isinstance(r, (dict, list)):
        return r
    if isinstance(r, tuple):
        r = r[-1]
    if isinstance(r, (bytes, bytearray)):
        r = r.decode("utf-8")
    return json.loads(r) if isinstance(r, str) else r


def _ai(chunk, src, target) -> List[str]:
    from ..ai import config as aicfg, keystore
    from ..ai.providers import make
    cfg = aicfg.load()
    sid = cfg.get("service") or ""
    if not sid:
        raise OnlineError("No AI service is set up (Options > AI).")
    p = make(sid, key=keystore.get_key(sid) or "", model=aicfg.model_for(cfg, sid),
             base_url=aicfg.base_url_for(cfg, sid))
    system = ("You translate ECU calibration labels from %s to %s. Keep "
              "identifiers, numbers, units and abbreviations as they are. Answer "
              "with a JSON array of strings only, one per input, in order."
              % (src or "the source language", target))
    turn = p.complete(system, [{"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}],
                      [], max_tokens=8192)
    m = re.search(r"\[.*\]", turn.text or "", re.S)
    if not m:
        raise OnlineError("the AI service did not answer with a list")
    out = json.loads(m.group(0))
    return [str(x) for x in out]


# ------------------------------------------------------------------ applying
def fields(defn):
    """Every translatable text: ``[(getter, setter, kind, table)]``."""
    out = []
    for t in defn.tables:
        display_name = not t.symbol or t.symbol != t.name
        if display_name:
            out.append((t, "name", "text"))
        out.append((t, "description", "text"))
        out.append((t, "units", "units"))
        for ax in (t.x, t.y):
            if ax is not None:
                out.append((ax, "name", "text"))
                if ax.values is not None:
                    out.append((ax.values, "units", "units"))
    return out


def plan(defn, opts: Dict, warnings: Optional[List[str]] = None,
         on_progress: Optional[Callable] = None) -> List[dict]:
    """Every text of ``defn`` with its translation and where it came from:
    ``[{"table", "field", "source", "result", "by"}]`` -- what the review
    grid shows. Nothing in ``defn`` changes."""
    warnings = warnings if warnings is not None else []
    src, tgt = opts.get("source", "auto") or "auto", opts.get("target", "en") or "en"
    pair = "%s>%s" % (src, tgt)
    g = glossary(src, tgt)
    tm = memory().get(pair, {})
    rows: List[dict] = []
    todo: Dict[str, List[dict]] = {}

    def add(table, fld, text, kind):
        if not text or not str(text).strip():
            return
        text = str(text)
        row = {"table": table, "field": fld, "source": text, "result": None,
               "by": "", "kind": kind}
        hit = g.get(text) or g.get(text.strip())
        if hit is not None:
            row["result"], row["by"] = hit, "glossary"
        elif text in tm:
            row["result"], row["by"] = tm[text], "memory"
        elif src in ("de", "auto") and tgt == "en":
            en = builtin(text, kind)
            if en is not None:
                row["result"], row["by"] = en, "built-in"
        if row["result"] is None and kind != "units":
            todo.setdefault(text, []).append(row)
        rows.append(row)

    seen_cats = set()
    for t in defn.tables:
        if not t.symbol or t.symbol != t.name:
            add(t.name, "name", t.name, "text")
        add(t.name, "description", t.description, "text")
        add(t.name, "units", t.z.units, "units")
        for tag, ax in (("x", t.x), ("y", t.y)):
            if ax is None:
                continue
            add(t.name, tag + "_name", ax.name, "text")
            if ax.values is not None:
                add(t.name, tag + "_units", ax.values.units, "units")
            if ax.labels and (ax.values is None or ax.values.addr is None):
                for i, lab in enumerate(ax.labels):
                    add(t.name, "%s_label_%d" % (tag, i), lab, "text")
        for c in t.categories:
            if c not in seen_cats:
                seen_cats.add(c)
                add("", "category", c, "category")
    if todo and opts.get("backend") == "online":
        provider = opts.get("provider") or settings().get("provider", "libretranslate")
        texts = list(todo)
        if on_progress:
            on_progress("Translating %d texts online (%s)" % (len(texts), provider))
        try:
            res = online(texts, provider, src, tgt)
        except OnlineError as exc:
            warnings.append("Online translation failed, built-in results kept: %s" % exc)
            res = None
        if res:
            got = {}
            for text, en in zip(texts, res):
                for row in todo[text]:
                    row["result"], row["by"] = en, provider
                got[text] = en
            remember(pair, got)
    for r in rows:
        if r["result"] is None:
            r["result"], r["by"] = r["source"], ""
        if r["by"] != "glossary":
            # glossary terms still standing in a result (a word the
            # translator kept) take their settled English
            hit = _glossary_hit(r["result"], g)
            if hit is not None and hit != r["result"]:
                r["result"] = hit
                r["by"] = (r["by"] + "+glossary").lstrip("+")
    return rows


def online_cost(rows: List[dict]) -> Tuple[int, int]:
    """``(texts, characters)`` that would go to an online translator."""
    texts = {r["source"] for r in rows if not r["by"] and r.get("kind") != "units"}
    return len(texts), sum(len(t) for t in texts)


def apply(defn, opts: Dict, warnings: Optional[List[str]] = None,
          rows: Optional[List[dict]] = None) -> int:
    """Translate ``defn`` in place (``rows`` from :func:`plan`, possibly
    edited in the review grid). Returns how many texts changed."""
    warnings = warnings if warnings is not None else []
    if rows is None:
        rows = opts.get("rows")          # reviewed in the window's grid
    rows = rows if rows is not None else plan(defn, opts, warnings)
    mode = opts.get("mode", "replace")
    by_table: Dict[str, Dict[str, str]] = {}
    cats: Dict[str, str] = {}
    for r in rows:
        if r["result"] == r["source"]:
            continue
        if r["field"] == "category":
            cats[r["source"]] = r["result"]
        else:
            by_table.setdefault(r["table"], {})[r["field"]] = r["result"]

    def put(orig: str, new: str) -> str:
        if mode == "bilingual":
            return "%s / %s" % (orig, new)
        return new

    n = 0
    for t in defn.tables:
        ch = by_table.get(t.name)
        name0 = t.name
        if ch:
            for fld, new in ch.items():
                if fld == "name":
                    t.originals["name"] = t.name
                    t.name = put(t.name, new)
                elif fld == "description":
                    t.originals["description"] = t.description
                    t.description = put(t.description, new)
                    if mode == "keep":
                        t.description = new + " | Original: " + t.originals["description"]
                elif fld == "units":
                    t.z.units = new
                else:
                    tag, _sep, rest = fld.partition("_")
                    ax = t.x if tag == "x" else t.y
                    if ax is None:
                        continue
                    if rest == "name":
                        ax.name = put(ax.name, new)
                    elif rest == "units" and ax.values is not None:
                        ax.values.units = new
                    elif rest.startswith("label_") and ax.labels:
                        i = int(rest[6:])
                        if i < len(ax.labels):
                            ax.labels[i] = new
                n += 1
            if mode == "keep" and "name" in ch and t.symbol != name0:
                t.description = (t.description + " | Original name: " + name0).strip(" |")
        if cats:
            t.categories = [cats.get(c, c) for c in t.categories]
    n += len(cats)
    return n
