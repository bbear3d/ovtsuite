# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What every AI service adapter shares: the provider-neutral conversation,
one turn's result, usage, errors, and a small JSON-over-HTTPS client on the
standard library (no vendor SDKs -- see docs/AI_PLAN.md).

The neutral conversation is a list of dicts:

* ``{"role": "user", "text": str}``
* ``{"role": "assistant", "text": str, "calls": [Call...], "raw": <wire>}``
  -- ``raw`` is the provider's own message, sent back unchanged so anything
  it must see again (Gemini's thought signatures, say) survives;
* ``{"role": "tool", "results": [{"id", "name", "content", "is_error"}]}``
"""

from __future__ import annotations

import json
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Kinds of failure the assistant reacts to differently.
OFFLINE, AUTH, RATE, SERVER, BAD = "offline", "auth", "rate", "server", "bad_request"
#: refused by a firewall / CDN in front of the service, not by the service
BLOCKED = "blocked"


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class Call:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cached: int = 0                     # input tokens read from a cache

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cached += other.cached


@dataclass
class Turn:
    text: str = ""
    calls: List[Call] = field(default_factory=list)
    raw: Any = None
    usage: Usage = field(default_factory=Usage)
    stop: str = ""


@dataclass
class Spec:
    """A service the user can pick (a provider adapter + defaults)."""
    id: str
    label: str
    kind: str                            # "anthropic" | "openai" | "gemini"
    base_url: str
    needs_key: bool = True
    local: bool = False
    key_url: str = ""                    # where to get a key
    env: str = ""                        # environment variable with a key
    note: str = ""                       # free tier, privacy ...


class HttpClient:
    """POST/GET JSON. ``on_send(record)`` sees every request (without the
    key) for the "what was sent" log."""

    def __init__(self, timeout: float = 120.0, on_send=None):
        self.timeout = timeout
        self.on_send = on_send

    def request(self, method: str, url: str, headers: Dict[str, str],
                body: Optional[dict] = None, log_url: Optional[str] = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": user_agent()}
        h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        t0 = time.time()
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": method,
               "url": log_url or url, "body": body}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=_ssl_context()) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as ex:
            text = ex.read().decode("utf-8", "replace")
            rec.update(status=ex.code, error=text[:2000], seconds=round(time.time() - t0, 2))
            self._log(rec)
            raise _http_error(ex.code, text)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as ex:
            reason = getattr(ex, "reason", ex)
            rec.update(error=str(reason), seconds=round(time.time() - t0, 2))
            self._log(rec)
            raise ProviderError(OFFLINE, "could not reach %s: %s" % (
                urllib.request.urlparse(url).netloc, reason))
        try:
            out = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ProviderError(SERVER, "the service answered with something "
                                "that is not JSON (HTTP %d)" % status)
        rec.update(status=status, seconds=round(time.time() - t0, 2),
                   response=_brief_response(out))
        self._log(rec)
        return out

    def stream(self, method: str, url: str, headers: Dict[str, str],
               body: Optional[dict] = None, log_url: Optional[str] = None):
        """Open a streamed request. Returns ``("json", obj)`` when the server
        answered with an ordinary JSON body (it ignored the stream request),
        else ``("sse", events)``: a generator of ``(event, data)`` pairs of
        the server-sent-event stream. The request is logged when the stream
        ends."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = {"Content-Type": "application/json", "Accept": "text/event-stream",
             "User-Agent": user_agent()}
        h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        t0 = time.time()
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": method,
               "url": log_url or url, "body": body, "stream": True}
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context())
        except urllib.error.HTTPError as ex:
            text = ex.read().decode("utf-8", "replace")
            rec.update(status=ex.code, error=text[:2000], seconds=round(time.time() - t0, 2))
            self._log(rec)
            raise _http_error(ex.code, text)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as ex:
            reason = getattr(ex, "reason", ex)
            rec.update(error=str(reason), seconds=round(time.time() - t0, 2))
            self._log(rec)
            raise ProviderError(OFFLINE, "could not reach %s: %s" % (
                urllib.request.urlparse(url).netloc, reason))
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype:
            raw = resp.read()
            resp.close()
            try:
                out = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                raise ProviderError(SERVER, "the service answered a stream request with "
                                    "something that is neither a stream nor JSON")
            rec.update(status=resp.status, seconds=round(time.time() - t0, 2),
                       response=_brief_response(out))
            self._log(rec)
            return "json", out

        def events():
            ev, buf, n = "", [], 0
            try:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if buf:
                            n += 1
                            yield ev or "message", "\n".join(buf)
                        ev, buf = "", []
                        continue
                    if line.startswith(":"):
                        continue
                    k, _, v = line.partition(":")
                    v = v[1:] if v.startswith(" ") else v
                    if k == "event":
                        ev = v
                    elif k == "data":
                        buf.append(v)
                if buf:
                    n += 1
                    yield ev or "message", "\n".join(buf)
            except (socket.timeout, OSError) as ex:
                raise ProviderError(OFFLINE, "the stream was cut off: %s" % ex)
            finally:
                resp.close()
                rec.update(status=resp.status, seconds=round(time.time() - t0, 2),
                           response={"streamed_events": n})
                self._log(rec)
        return "sse", events()

    def _log(self, rec):
        if self.on_send is not None:
            try:
                self.on_send(rec)
            except Exception:
                pass


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        pass
    return ctx


def _brief_response(out):
    """What the log keeps of a reply: everything but bulky lists."""
    if isinstance(out, dict) and isinstance(out.get("data"), list) and len(out["data"]) > 20:
        return dict(out, data="[%d items]" % len(out["data"]))
    return out


def user_agent() -> str:
    """Who is calling. Python's own ``Python-urllib/3.x`` is on the block
    lists of some services' firewalls (Groq's Cloudflare answers it with
    403 / error 1010, which looks like a refused key), so name the app."""
    from ... import branding
    return "%s/%s (%s; Python %s)" % (branding.APP_NAME.replace(" ", ""), branding.__version__,
                                      platform.system() or "unknown",
                                      platform.python_version())


def _firewall_block(code: int, text: str, j) -> Optional[str]:
    """A refusal by the CDN in front of the service (Cloudflare and the
    like), not by the service: what to tell the user, else None."""
    if code not in (403, 429, 503) or not text:
        return None
    low = text[:4000].lower()
    if isinstance(j, dict) and ("cloudflare_error" in j or j.get("error_category") == "access_denied"
                                or str(j.get("error_code", "")).startswith("10")):
        what = j.get("error_name") or j.get("title") or "access denied"
        return ("the service's firewall blocked the request before it reached the "
                "service (%s, HTTP %d) -- this is not about the API key" % (what, code))
    if "cloudflare" in low and ("<html" in low or "error code" in low or "ray id" in low):
        return ("the service's firewall (Cloudflare) blocked the request before it reached "
                "the service (HTTP %d) -- this is not about the API key" % code)
    return None


def _http_error(code: int, text: str) -> ProviderError:
    msg = text
    j = None
    try:
        j = json.loads(text)
        e = j.get("error", j) if isinstance(j, dict) else j
        if isinstance(e, dict):
            msg = e.get("message") or json.dumps(e)
        elif isinstance(e, str):
            msg = e
    except ValueError:
        pass
    fw = _firewall_block(code, text, j)
    if fw:
        return ProviderError(BLOCKED, fw, code)
    msg = (msg or "").strip()
    if msg.lstrip().startswith("<"):                   # an HTML error page
        import re
        t = re.search(r"<title[^>]*>(.*?)</title>", msg, re.I | re.S)
        msg = (t.group(1).strip() if t else "an HTML error page")
    msg = msg[:300]
    if code in (401, 403):
        return ProviderError(AUTH, "the service refused the API key (%d): %s" % (code, msg), code)
    if code == 429:
        return ProviderError(RATE, "rate limit or quota reached: %s" % msg, code)
    if code >= 500 or code == 529:
        return ProviderError(SERVER, "the service had a problem (%d): %s" % (code, msg), code)
    return ProviderError(BAD, "the service rejected the request (%d): %s" % (code, msg), code)


class Provider:
    """One configured service: spec + key + model."""

    kind = ""

    def __init__(self, spec: Spec, key: str = "", model: str = "",
                 base_url: str = "", http: Optional[HttpClient] = None):
        self.spec = spec
        self.key = key or ""
        self.model = model
        self.base_url = (base_url or spec.base_url).rstrip("/")
        self.http = http or HttpClient()

    @property
    def label(self) -> str:
        return self.spec.label

    def list_models(self) -> List[str]:
        raise NotImplementedError

    def complete(self, system: str, messages: List[dict], tools: List[dict],
                 max_tokens: int = 4096, on_text=None) -> Turn:
        """One model turn. With ``on_text`` the reply is streamed and every
        piece of text is passed to it as it arrives; a service that does not
        stream is asked again without streaming (and not asked to stream
        again)."""
        if on_text is not None and not getattr(self, "_no_stream", False):
            try:
                return self.stream_turn(system, messages, tools, max_tokens, on_text)
            except NotImplementedError:
                pass
            except ProviderError as ex:
                if ex.kind != BAD:
                    raise
                # a server without SSE support -- or a request the service
                # refuses either way: only stop streaming when the plain
                # request is accepted (then the stream was the problem)
                turn = self.turn(system, messages, tools, max_tokens)
                self._no_stream = True
                return turn
        return self.turn(system, messages, tools, max_tokens)

    def turn(self, system, messages, tools, max_tokens=4096) -> Turn:
        raise NotImplementedError

    def stream_turn(self, system, messages, tools, max_tokens, on_text) -> Turn:
        raise NotImplementedError

    def check(self) -> str:
        """A cheap request that proves the key and the address work: the
        model list. Returns a one-line status; raises ProviderError."""
        models = self.list_models()
        return "connected: %d model%s available" % (len(models), "" if len(models) == 1 else "s")


def tool_result_text(content) -> str:
    return content if isinstance(content, str) else json.dumps(
        content, ensure_ascii=False, separators=(",", ":"), default=str)
