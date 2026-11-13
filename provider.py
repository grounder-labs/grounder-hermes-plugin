# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder as a Hermes web search and extract backend.

Each ``web_search`` or ``web_extract`` call is one ``POST /v1/search`` to Grounder
and counts as one search. A search makes one call to one provider and returns
that provider's own licensed results, mapped to Hermes's rows (title,
URL, description, position). An extract reads the pages it is given and returns each
page's text as Grounder returns it, read for the question of the ``web_search`` that found it. Every mode returns
the provider's own results: this plugin cuts, reranks and reorders nothing of them, and asks Grounder for no ``text_max_chars``, passage
or rerank option, so a row's description and a page's text are Grounder's words, with only white space on a row's line
normalised. Hermes itself applies a model's ``char_limit`` to a whole page. Every failure is
reported, never raised: ``{"success": False, "error": ...}`` for a search, the
page's own ``error`` for an extract. A failure the user must act on (a limit reached, a key or
sign-in Grounder no longer accepts) is reported as a result that carries Grounder's message (``_notice``),
because Hermes serves a failed search from its keyless ring of other vendors' results, and the message would never reach the user.

A result Grounder served asks the model to cite what it uses by number, and an
answer ends with the Sources list of what it cites and, when it searched, the line
Grounder's card shows (``after_tool`` and ``after_answer``, Hermes's result and reply
hooks). Both work on what is already on this machine and send nothing. A
``web_search`` whose query holds a page's address reads that page for the rest of
the query, and the page comes back whole.

Grounder has three modes: Grounder optimized (Grounder picks the provider for the question: the default), Grounder preset (the user
chose Fast, Cheap, Live, Deep, Code or Balanced, and Grounder picks the best provider for it) and Grounder direct (the user chose one
provider, and optionally its mode: only that provider). A search carrying the user's choices (a key or a sign-in) names its mode in
the request's ``mode`` (``_send``), and the line under an answer names the mode of the searches it used (``_card_line``). A Grounder
from before 0.9.0 ignores ``mode`` and maps the same fields (``profile``, an exclusive pin) itself.

The plugin selects itself the first time it loads in a profile (``__init__.py``), by
setting ``web.search_backend: grounder`` and ``web.extract_backend: grounder``.
A call uses the Grounder search key in ``GROUNDER_API_KEY`` when one is set, and otherwise the sign-in Hermes keeps
for the ``oauth:`` client in ``plugin.yaml`` (``hermes auth add grounder:grounder``):
Hermes signs in, stores and refreshes that grant and attaches its token to the
request, so this plugin never holds a token. With neither, a search is one of Grounder's free searches: it carries no
credential, and neither the server nor the mode the user chose, nor a profile, routing settings or workspace the model asked for, which Grounder refuses a caller
with no account. A free search that reaches Grounder's limit says where to sign up and add credit, with the links the API gave. No request the plugin makes follows
a redirect (``_open``), and no address that carries a key or is not public is sent (``_refusal``).

``/search`` and ``/search_presets`` (``commands.py``) save the provider, its mode and the preset to ``web.grounder.server``,
``web.grounder.server_mode`` and ``web.grounder.preset``, the same keys Hermes's own Grounder provider reads, and a signed-in
``web_search`` or ``grounder_search`` sends them (``chosen``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx

from agent.secret_scope import get_secret
from agent.web_search_provider import WebSearchProvider
from hermes_cli.auth_constants import AuthError
from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request

logger = logging.getLogger(__name__)

ORIGIN = "https://api.grounder.net"
SEARCH_URL = f"{ORIGIN}/v1/search"
FEATURES_URL = f"{ORIGIN}/v1/features"
PROVIDERS_URL = f"{ORIGIN}/v1/providers"  # public: the search providers and the live-data APIs, which /search offers
PROFILES_URL = f"{ORIGIN}/v1/search-profiles"  # public: the presets, which /search_presets offers
OAUTH_ID = "grounder"  # the id of the oauth: entry in plugin.yaml
SIGN_IN = f"hermes auth add grounder:{OAUTH_ID}"  # the plugin's key (its name), a colon, the entry's id
USER_AGENT = "grounder-hermes/0.7.1"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
KEY_ENV = "GROUNDER_API_KEY"
CLIENT_ENV = "GROUNDER_OAUTH_CLIENT_ID"  # filled in at the first load when there is no search key
CLIENT_ID = f"{ORIGIN}/oauth/clients/hermes-agent"  # public, the same for everyone
MAX_RESULTS = 100  # most results one search asks Grounder for: its top_k range, and the 100 Hermes clamps a model's limit to
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
MODES = ("optimized", "preset", "direct")  # Grounder's three modes (POST /v1/search ``mode``, Grounder 0.9.0)
# A direct search returns only what the provider the user chose gave, even when that is nothing (Harlan, 2026-11-12: "user does for direct"): it sends `fallback: false`.
# True lets Grounder pick another provider when that one finds nothing.
DIRECT_FALLBACK = False
MAX_PAGES = 10  # Grounder's limit on the pages one call reads; Hermes asks for at most 5
REMEMBERED_PAGES = 500  # how many pages the plugin remembers the web_search of, in memory only
REMEMBERED_TURNS = 200  # how many searches and unfinished turns it remembers for the line under an answer, in memory only
# Two notes the model reads once an answer, on the first of the turn's results Grounder served (GRO-698): every search of a turn repeating them cost
# about 680 characters a search, and an answer needs them once. `after_tool` adds them, as the result's last fields, so the citation request is
# the last thing read; a host that names no turn gets them on every result.
CITE_KEY = "cite_sources"
# The model cites by number and `after_answer` writes the Sources list from what Grounder served (GRO-701): a list of titles and links was a third of an
# answer, in the model's slowest and dearest tokens, and the plugin already holds every word of it. A result's `position` is its number across the turn, and an
# answer cites only its own turn's results (Harlan, 2026-11-11, GRO-835: "Follow ups should only cite sources from their respective response"): the next answer
# that searches starts again at [1], and one that searched nothing cites nothing by number, so the model must never reuse an earlier answer's number.
CITE_REQUEST = ("Cite every result your answer uses by its position in square brackets, like [3]. Positions belong to this answer alone: they run on across "
                "this answer's searches and page reads, and start again at [1] in the next answer that searches. Cite only results this answer's own searches "
                "and page reads returned: an answer that made no search cites nothing by number, and never reuses a number from an earlier answer, so search "
                "again when you need a source. "
                "End the answer with its last sentence: write no Sources, References or Citations list and no links, since Grounder adds the list of "
                "every cited result's title and link under the answer, and one you write costs tokens for nothing.")
# For a host that names no turn the plugin cannot tell which answer a result serves, so the model writes the list itself. The first sentence is word for
# word what Grounder's MCP server ends every search's notes with (CITE_REMINDER in routes/mcp.ts).
CITE_REQUEST_LISTED = ("Cite every result your answer uses, each with its link, in this answer and in every later one in this chat, not only the first. "
                       "End the answer with a Sources list: one line per result used, with its title and its link.")
# A page read that failed, for a page a search returned: the model gets what that search showed of it (GRO-694). In test-b a bare error sent the model
# to fetch the page itself, and Hermes wrote itself a skill to do so whenever Grounder erred (GRO-688).
SHOWN_NOTE = "Grounder could not read this page in full. This is what its search showed of it."
ADDRESS = re.compile(r"https?://[^\s<>\"'`]+", re.I)
# A heading or label that starts a Sources list the model wrote itself, so the plugin adds none: the word alone on its line (`Sources`, `## Sources`,
# `**Sources**`), or as a label with a colon (`Sources:`, `**Sources:** [1] ...`). A sentence that begins with the word ("Sources differ slightly ...") is not one.
LISTED = re.compile(r"^[ \t>]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?(?:sources|references|citations)(?:\*\*|__)?[ \t]*(?::|$)", re.I | re.M)
# A citation: [3], [3, 5] or [3-5].
CITED = re.compile(r"\[(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\]")
# Code, which is never a citation: a fenced block (to its closing fence, or the end of the text) and an inline span (`items[2]`).
CODE = re.compile(r"(?s)(?:```|~~~).*?(?:```|~~~|\Z)|(`+)(?!`)(?:(?!\n\n).)+?(?<!`)\1(?!`)")
SEARCH_TOOL = "grounder_search"
LIST_PROVIDERS_TOOL = "grounder_list_providers"
TOOLSET = "grounder"
# The options of Grounder's MCP search tool, in its words, for what Hermes's web_search cannot say: it takes a query and a limit.
SEARCH_SCHEMA = {
    "name": SEARCH_TOOL,
    "description": "Search the web or read pages through Grounder with more control than web_search: recent results only, named sites, "
                   "the features the answer needs, pages to read, a named provider, a workspace or this search's routing settings. "
                   "For a plain search use web_search. A call counts as one search.",
    "parameters": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "The search query."},
        "top_k": {"type": "integer", "description": "How many results (1-100). Default 10."},
        "days": {"type": "integer", "description": "Only results published within this many days."},
        "domains": {"type": "array", "items": {"type": "string"}, "description": 'Restrict to these domains, e.g. ["bbc.com"].'},
        "features": {"type": "array", "items": {"type": "string"},
                     "description": f'The features the answer needs ({LIST_PROVIDERS_TOOL} lists them), e.g. ["news"] or ["stock-quote"]. Left out, Grounder works them out from the query.'},
        "urls": {"type": "array", "items": {"type": "string"},
                 "description": "Pages to read (http(s) addresses): links the user gave, or a result whose text is cut short. Put what to look for in query."},
        "include_text": {"type": "boolean", "description": "Include passage text. Default true."},
        "requested_provider": {"type": "string", "description": f"Provider the user explicitly asked for (Grounder direct), by id ({LIST_PROVIDERS_TOOL} lists them)."},
        "providers": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 1, "description": "The one provider to call (Grounder direct): an id as for requested_provider."},
        "routing_settings": {"type": "object", "description": "This search's routing settings over the account's saved ones; not saved. Pass only what differs, "
                                                              'when the user or the task calls for it, e.g. {"providers": {"exa": {"use": "always"}}}. What the user chose with /search '
                                                              "(a live-data API turned off, a provider or mode they chose to search with alone) wins over what you pass for the same provider or API, so you cannot switch one back on. "
                                                              "Needs the user to be signed in to Grounder."},
        "workspace": {"type": "string", "description": "Slug of the workspace to search under; the default one when left out. An unknown slug is refused, never replaced by the default. Needs the user to be signed in to Grounder."},
        "profile": {"type": "string", "description": f"A search preset by id (Grounder preset), which changes how this search runs ({LIST_PROVIDERS_TOOL} lists them under search_modes); `auto` is none. "
                                                      "Left out, the user's own choice stands, and a provider the user chose to search with alone stands over any preset. Needs the user to be signed in to Grounder."},
        "user_location": {"type": "string", "description": 'Where the user is, only if the user said so in this chat, e.g. "Austin, Texas". Never guess it.'},
        "user_time_zone": {"type": "string", "description": "The user's time zone, only if the user said so in this chat, as an IANA name, e.g. \"America/Chicago\". Never guess it."},
    }},
}
LIST_PROVIDERS_SCHEMA = {
    "name": LIST_PROVIDERS_TOOL,
    "description": "List the features a Grounder search can use: what each returns, the inputs it honors, and the providers (with modes) and live-data APIs that deliver it, "
                   "and the presets a search can run in.",
    "parameters": {"type": "object", "properties": {}},
}
# Where a provider's or live source's id is not its name capitalized; the rest of Grounder's lists are.
PROVIDER_NAMES = {"you": "You.com", "xai": "xAI", "newsapi": "NewsAPI.ai", "tinyfish": "TinyFish", "aeroapi": "FlightAware",
                  "ap-elections": "AP Elections", "bestbuy": "Best Buy", "espn": "ESPN", "fed": "Federal Reserve", "mlb": "MLB",
                  "nws": "National Weather Service", "odds-api": "The Odds API", "openf1": "OpenF1", "queue-times": "Queue-Times.com",
                  "seatgeek": "SeatGeek", "statuspage": "Status pages", "truth-social": "Truth Social", "usgs": "USGS",
                  "yahoo-finance": "Yahoo Finance", "youtube": "YouTube"}
TIMEOUT_S = 45  # one search, across every provider it reaches
LIST_TIMEOUT_S = 8  # one of Grounder's public lists
KEYS_URL = "https://grounder.net/account/keys"
SETTINGS_KEY = "grounder"  # the section of config.yaml's web: that /search and /search_presets write
LIVE_OFF_KEY = "live_data_off"  # in that section: the live-data APIs the user turned off (a list of ids), so a new one is on
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SERVER = re.compile(r"^(provider|api):([a-z0-9][a-z0-9._-]*)$")
MAX_FORGET = 4  # the choices one search may drop: a 400 costs nothing, but the user's whole saved list is not a loop to walk


def _bounded(store: dict, limit: int) -> None:
    """Drop the oldest entries of ``store`` past ``limit``."""
    while len(store) > limit:
        del store[next(iter(store))]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _duration(ms: int) -> str:
    """A search's time as Grounder's card writes it."""
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


def _provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider.capitalize())


def _row(result: dict[str, Any], position: int) -> dict[str, Any]:
    """Hermes's row for one result, with its date when Grounder has one.

    A result's provenance (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's records and in the API's
    response, and the line under an answer names the providers; the model reads none of it per result (GRO-698: about 100 characters a result).
    """
    date = str(result.get("published_date") or "")[:10]
    return {"title": str(result.get("title") or ""), "url": str(result.get("url") or ""),
            "description": _description(result), "position": position, **({"date": date} if date else {})}


def _listed(value: Any) -> list[Any]:
    """``value`` when it is a list, else none: a response whose list is another shape is read as having none."""
    return value if isinstance(value, list) else []


def _results(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The result rows of a response: each one that is an object with an address (every result has one). A null among them, a row with none, or a ``results``
    that is not a list, never fails the search."""
    return [result for result in _listed(body.get("results")) if isinstance(result, dict) and isinstance(result.get("url"), str) and result["url"].strip()]


# Where this process reads the time of a search, so the time under an answer can be tested.
_clock = time.monotonic


def _mode_name(body: dict[str, Any], sent: Optional[dict[str, Any]]) -> str:
    """The mode a search ran in as the line under an answer names it: ``Grounder optimized``, ``Grounder preset (Fast)`` or ``Grounder direct``, or "" when
    nothing says. The response's own ``routing.mode`` (or a top-level ``mode``) when it is one of Grounder's three, else the ``mode`` of the request that was sent. A
    preset carries the id of the preset that ran, capitalised: ``routing.profile``, else the ``profile`` that was sent. Every field may be absent (a Grounder from before 0.9.0)."""
    sent = sent or {}
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    mode = next((value for value in (routing.get("mode"), body.get("mode"), sent.get("mode")) if isinstance(value, str) and value in MODES), "")
    if not mode:
        return ""
    named = next((value.strip() for value in (routing.get("profile"), sent.get("profile")) if isinstance(value, str) and value.strip() not in ("", "auto")), "")
    return f"Grounder {mode}" + (f" ({named.capitalize()})" if mode == "preset" and named else "")


def _summary(body: dict[str, Any], sources: int, sent: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """What the line under an answer needs of one search: its mode, who answered, how long it took and when it ran. Who answered is ``routing.provider`` (Grounder 0.9.0)
    when the response names one, else the providers whose calls returned results.

    ``sources`` is how many results the plugin got, which says whether it found any; the line counts the ones the model was shown (``after_tool``).
    ``sent`` is the request that was sent, which names the mode when the response does not (``_mode_name``).
    """
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    calls = [*_listed(routing.get("calls")), *_listed(routing.get("live_calls"))]
    answered = [str(c.get("provider")) for c in calls if isinstance(c, dict) and c.get("status") == "ok" and c.get("result_count")]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    latency = usage.get("latency_ms")
    ms = latency if isinstance(latency, int) and not isinstance(latency, bool) else 0
    named = routing.get("provider")
    via = [_provider_name(named.strip())] if isinstance(named, str) and named.strip() else [_provider_name(p) for p in dict.fromkeys(answered)]
    return {"mode": _mode_name(body, sent), "via": via, "sources": sources, "ms": ms,
            "from": _clock() - ms / 1000, "fresh": True}


def _waited(searches: list[dict[str, Any]]) -> int:
    """The milliseconds the searches of one answer took, with the time two that ran together shared counted once."""
    spans = sorted((search["from"], search["from"] + search["ms"] / 1000) for search in searches if search["ms"])
    total, end = 0.0, None
    for start, finish in spans:
        start = start if end is None else max(start, end)
        total += max(0.0, finish - start)
        end = finish if end is None else max(end, finish)
    return round(total * 1000)


def _card_line(searches: list[dict[str, Any]]) -> str:
    """The line Grounder's card shows for a search, for every search of one answer: the mode, the providers that answered, the sources the model was shown and
    the time. Searches of one answer that ran in different modes name each once; with no mode known it says only Grounder."""
    modes = list(dict.fromkeys(search["mode"] for search in searches if search.get("mode")))
    via = list(dict.fromkeys(name for search in searches for name in search["via"]))
    parts = ["Searched with " + (", ".join(modes) or "Grounder")]
    if via:
        parts.append("via " + ", ".join(via[:2]) + (f" +{len(via) - 2}" if len(via) > 2 else ""))
    parts.append(_plural(len(set().union(*(search["pages"] for search in searches))), "source"))
    total = _waited(searches)
    if total:
        parts.append(_duration(total))
    return " · ".join(parts)


def _cited(text: str) -> set[int]:
    """The numbers an answer cites in square brackets, a range counting each number in it."""
    numbers: set[int] = set()
    for group in CITED.findall(CODE.sub(" ", text)):
        for part in re.split(r"\s*,\s*", group):
            bounds = [int(bound) for bound in re.split(r"\s*[–-]\s*", part) if bound.isdigit()]
            if len(bounds) == 2 and bounds[0] <= bounds[1] <= bounds[0] + 50:
                numbers.update(range(bounds[0], bounds[1] + 1))
            elif len(bounds) == 1:
                numbers.add(bounds[0])
    return numbers


_MARKDOWN = re.compile(r"([\\\[\]()*_`#<>|~!])")
_UNSEEN = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")  # control, format (zero-width, right-to-left), surrogate, private, unassigned and line-separator characters


def _plain(title: str) -> str:
    """A title as one line of plain words: no control, zero-width or direction characters, and no markdown, so a page's title cannot add a link, a heading or a list to the answer."""
    text = " ".join("".join(" " if char.isspace() else char for char in title if char.isspace() or unicodedata.category(char) not in _UNSEEN).split())
    return _MARKDOWN.sub(r"\\\1", text)


def _link(url: str) -> str:
    """An address as one safe token, or "" when it is not a web address: only http(s) is listed, and a space, quote, angle bracket or invisible character in it is percent-encoded."""
    if not re.match(r"https?://[^/?#\s]", url, re.I):
        return ""
    return "".join("".join(f"%{byte:02X}" for byte in char.encode()) if char in ' <>"`\\{}|^' or unicodedata.category(char) in (*_UNSEEN, "Zs") else char for char in url)


def _sources_list(text: str, sources: dict[str, dict[str, Any]]) -> str:
    """The Sources list under an answer: each result it cites by number, with its title and link. None when it wrote its own or cites none.

    A title and an address are a page's own words, so the list prints them defanged (``_plain``, ``_link``): a result is never listed under a ``javascript:``,
    ``file:`` or ``data:`` address, and a title cannot carry markup into the answer the user reads.
    """
    if not sources or LISTED.search(text):
        return ""
    cited = _cited(text)
    used = sorted((source for source in sources.values() if source["n"] in cited), key=lambda source: source["n"])
    # One plain bullet each, in the order of their numbers (Harlan, 2026-11-11, GRO-731: "a bullet point for each unnumbered").
    lines = [f"- {_plain(source['title']) or link} — {link}" for source in used if (link := _link(source["url"]))]
    return "Sources:\n" + "\n".join(lines) if lines else ""


def _fail(error: str) -> dict[str, Any]:
    """The failure result Hermes shows the model in place of rows."""
    return {"success": False, "error": error}


def _key() -> str:
    """The search key from this profile's secrets, or "" when none is set."""
    return str(get_secret(KEY_ENV) or "").strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. urllib re-sends a redirected request to its new address with the headers it had, the search key's ``Authorization`` among them, so a
    redirect to another host would hand that host the key. Hermes's own signed-in requests follow none either; a redirect from Grounder is an error here."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Every request the plugin makes itself goes through here, so none follows a redirect."""
    return _OPENER.open(request, timeout=timeout)


def _post(payload: dict[str, Any], key: str = "") -> tuple[int, bytes]:
    """Send one search, with the user's own search key or, with none, as a free search that carries no credential; return the HTTP status and the body."""
    request = urllib.request.Request(
        SEARCH_URL, json.dumps(payload).encode(),
        {**({"Authorization": f"Bearer {key}"} if key else {}), "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with _open(request, TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post_signed_in(payload: dict[str, Any]) -> tuple[int, bytes]:
    """Send one search through Hermes, which attaches the signed-in token; return the HTTP status and the body."""
    response = credentialed_provider_request(
        OAUTH_ID, "POST", SEARCH_URL, json=payload,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    return response.status, response.body


def _get(url: str) -> tuple[int, bytes]:
    """GET one of Grounder's public lists with no credential; return the HTTP status and the body."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with _open(request, LIST_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _get_signed_in(url: str) -> tuple[int, bytes]:
    """GET one of Grounder's public lists through Hermes, which raises ``ProviderNotSignedIn`` with no grant and otherwise attaches the token.
    A public list needs none; this is how a plugin, which cannot see the grant, learns there is one, with no search spent."""
    response = credentialed_provider_request(OAUTH_ID, "GET", url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=LIST_TIMEOUT_S)
    return response.status, response.body


def fetch_list(url: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One of Grounder's public lists, as it is when asked and never kept: ``(body, None)``, or ``(None, reason)``.

    Raises ``ProviderNotSignedIn`` when the user has neither a search key nor a sign-in: ``/search`` and ``/search_presets`` choose what only a signed-in search
    carries, so they answer with how to sign in instead of a list.
    """
    try:
        status, raw = _get(url) if _key() else _get_signed_in(url)
    except ProviderNotSignedIn:
        raise
    except AuthError as error:
        return None, f"Grounder sign-in failed: {error}"
    except PermissionError as error:
        return None, f"Hermes refused the Grounder sign-in: {error}"
    except (httpx.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        return None, f"Could not reach Grounder: {error}"
    if status != 200:
        return None, f"HTTP {status}"
    try:
        body = json.loads(raw)
    except ValueError:
        return None, "Grounder's answer was not JSON"
    return (body, None) if isinstance(body, dict) else (None, "Grounder's answer was not what was expected")


def settings() -> dict[str, Any]:
    """What ``/search`` and ``/search_presets`` saved in ``web.grounder``, read when asked: ``preset`` (a preset id), ``server`` (``provider:<id>``, or the
    older ``api:<id>``) and ``server_mode`` (one of that provider's modes), each ``""`` when unset, and ``off``, the live-data API ids the user turned off,
    in the order saved. A value of another shape reads as unset, and a config that cannot be read reads as nothing saved."""
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web")
    except Exception as error:  # a config that cannot be read must not fail a search
        logger.warning("Could not read the Grounder settings: %s", error)
        return {"preset": "", "server": "", "server_mode": "", "off": []}
    section = web.get(SETTINGS_KEY) if isinstance(web, dict) else None
    section = section if isinstance(section, dict) else {}
    preset, server, server_mode = (value.strip() if isinstance(value := section.get(name), str) else "" for name in ("preset", "server", "server_mode"))
    off = section.get(LIVE_OFF_KEY)
    return {"preset": preset, "server": server, "server_mode": server_mode,
            "off": list(dict.fromkeys(i.strip() for i in off if isinstance(i, str) and _ID.match(i.strip()))) if isinstance(off, list) else []}


def chosen(exclusive: bool = True) -> dict[str, Any]:
    """What the user chose with ``/search_presets`` and ``/search``, as the fields a search carries; ``{}`` when they chose nothing.

    The preset is the search's ``profile`` (Grounder preset). A pinned provider is an always-on setting for it, with the mode the user pinned always on too, and the search is
    exclusive (Grounder direct: ``exclusive: true``, Grounder 0.8.16): only that provider is called, and ``fallback`` (``DIRECT_FALLBACK``, off) says that when it finds nothing
    Grounder picks no other provider, whatever the account's own ``fallback`` is: the search returns what the provider gave, which may be nothing. With neither, the search is Grounder optimized. ``exclusive=False`` sends the older form, always included: Grounder may add other providers when the prompt needs them, for a
    Grounder that predates ``exclusive`` (``_send_chosen``). A pinned live-data API (the older way a server was saved) stays an always-on setting alone, as before. Each live-data
    API the user turned off is an off setting: it is the user's list of what is OFF, so a live-data API Grounder adds later is on until they turn it off. A pinned live-data
    API stays on whatever that list says. The keys are Hermes's own Grounder provider's, so a user who moves to it keeps its choices.
    """
    saved = settings()
    extra: dict[str, Any] = {}
    if _ID.match(saved["preset"]):
        extra["profile"] = saved["preset"]
    routing: dict[str, Any] = {}
    if match := _SERVER.match(saved["server"]):
        kind, ident = match.groups()
        pinned: dict[str, Any] = {"use": "always"}
        if kind == "provider" and _ID.match(saved["server_mode"]):
            pinned["modes"] = {saved["server_mode"]: "always"}
        routing["providers" if kind == "provider" else "specialized"] = {ident: pinned}
        if kind == "provider" and exclusive:
            routing.update({"exclusive": True, "fallback": DIRECT_FALLBACK})
    for ident in saved["off"]:
        routing.setdefault("specialized", {}).setdefault(ident, {"use": "off"})
    if routing:
        extra["routing_settings"] = routing
    return extra


def save_settings(**changes: Any) -> None:
    """Set or, with ``None`` or an empty list, remove keys of ``web.grounder`` in the active profile's config, in one write."""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    web = config.get("web") if isinstance(config.get("web"), dict) else config.setdefault("web", {})
    section = web.get(SETTINGS_KEY) if isinstance(web.get(SETTINGS_KEY), dict) else web.setdefault(SETTINGS_KEY, {})
    for key, value in changes.items():
        if value is None or value == []:
            section.pop(key, None)
        else:
            section[key] = value
    save_config(config)


# What Grounder's 400 says of a choice it no longer offers (workers/router/src: routing-settings.ts `applySettings`, search-profiles.ts `resolveProfile`).
_REFUSED = [(re.compile(r"No such search profile: (\S+?)\.?(?:\s|$)"), "preset"),
            (re.compile(r"No such provider: (\S+)"), "provider"),
            (re.compile(r"No such specialized API: (\S+)"), "api"),
            (re.compile(r"providers\.(\S+?)\.modes: no mode (\S+?);"), "mode")]
MAX_FORGET = 4  # the choices one search may forget: a 400 costs nothing, but the user's whole saved list is not a loop to walk


def _stale_choice(error: str, saved: dict[str, Any], sent: Optional[dict[str, Any]]) -> Optional[tuple[dict[str, Any], str]]:
    """``(what to save, what to tell the user)`` when Grounder's 400 names a saved choice it no longer offers, else None. Only a choice the user saved:
    what the model passed in this search (its own ``profile``, or a provider or live-data API in its ``routing_settings``) is the model's to fix."""
    if not error.startswith("Grounder returned HTTP 400"):
        return None
    sent = sent or {}
    routing = sent.get("routing_settings") if isinstance(sent.get("routing_settings"), dict) else {}

    def models(section: str, ident: str) -> bool:
        return isinstance(routing.get(section), dict) and ident in routing[section]

    for pattern, kind in _REFUSED:
        found = pattern.search(error)
        if not found:
            continue
        named = [name.rstrip(".") for name in found.groups()]
        ident = named[-1]
        server_kind, _, server = saved["server"].partition(":")
        if kind == "preset" and saved["preset"] == ident and not sent.get("profile"):
            return {"preset": None}, f"the preset `{ident}`"
        if kind == "provider" and server_kind == "provider" and server == ident and not models("providers", ident):
            return {"server": None, "server_mode": None}, f"the provider `{ident}` you chose to search with"
        if kind == "mode" and server_kind == "provider" and server == named[0] and saved["server_mode"] == ident and not models("providers", named[0]):
            return {"server_mode": None}, f"the `{ident}` mode of {named[0]} you chose to search with"
        if kind == "api" and server_kind == "api" and server == ident and not models("specialized", ident):
            return {"server": None}, f"the live-data API `{ident}` you always included"
        if kind == "api" and ident in saved["off"] and not models("specialized", ident):
            return {LIVE_OFF_KEY: [i for i in saved["off"] if i != ident]}, f"the live-data API `{ident}` you turned off"
    return None


ACT_CODES = ("daily_limit_reached", "capacity_reached", "cap_reached")  # a search refused for the user's own allowance, which only the user can change
GROUNDER_SITE = "https://grounder.net/account"
NOTE_KEY = "notice"  # on a search that ran: what Grounder wants the user told about it (a choice of theirs it no longer offers, dropped for this search)
NOTICE_KEY = "grounder_notice"  # on a result that is Grounder's message to the user in place of rows: "act" (what the user must do first) or "refused"


class _Act(str):
    """A failure the user must act on: a limit reached, a key or sign-in Grounder no longer accepts. ``url`` is where to do it, when Grounder named one.

    It reads as the plain message. The provider answers it with a result that carries the message, not a failure (``_notice``): Hermes serves a failed
    search or read from its keyless ring, which hides the message from the user, and a transient failure is the only one it should cover.
    """

    url = ""


def _error_parts(body: Any) -> tuple[dict[str, Any], Optional[str], dict[str, Any]]:
    """The API's error object, its message and its details, each ``{}``/None when the body has none."""
    error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    detail = error.get("message") if isinstance(error.get("message"), str) else None
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return error, detail, details


def _must_act(status: int, body: Any, via: str) -> bool:
    """Whether a failed search is an answer the user must act on (a limit, a rejected key or sign-in), not a transient failure."""
    error, _detail, details = _error_parts(body)
    return status == 401 or (status in (429, 503) and (error.get("code") in ACT_CODES or (via == "free" and bool(details.get("signup_url")))))


def _error_message(status: int, body: Any, via: str) -> str:
    """A one-line message for a failed search, with Grounder's own reason when it gave one. ``via`` is how the search was made: ``key``, ``sign-in`` or ``free``."""
    error, detail, details = _error_parts(body)
    if via == "free" and status in (429, 503) and details.get("signup_url"):
        # The links are the API's own, and the limits are in no sentence here: Grounder changes them, this plugin does not.
        credit = f", then add credit at {details['billing_url']}" if details.get("billing_url") else ""
        used = "You've used Grounder's free searches" if status == 429 else "Grounder's free searches have reached their limit for now"
        return f"{used}. Sign up at {details['signup_url']}{credit}. Then sign in with `{SIGN_IN}`, or set a search key in `{KEY_ENV}`."
    if status == 401:
        if via == "sign-in":
            return f"Grounder rejected the sign-in. Run `{SIGN_IN}`."
        if via == "key":
            return f"Grounder rejected the search key in `{KEY_ENV}`. Check it at {KEYS_URL}, or remove it to search free."
        return f"Grounder refused a search with no account. Sign in with `{SIGN_IN}`, or set a search key from {KEYS_URL} in `{KEY_ENV}`."
    if status in (429, 503) and error.get("code") in ACT_CODES:
        return detail or "This Grounder account has reached its limit for now."  # Grounder's own words say when it resets and where to add credit
    if status == 429:
        return f"Grounder rate limit reached: {detail or 'try again later'}"
    if 300 <= status < 400:
        return f"Grounder answered HTTP {status}, a redirect, which this plugin does not follow."
    return f"Grounder returned HTTP {status}" + (f": {detail}" if detail else "")


def _act(message: str, url: str = "") -> _Act:
    failure = _Act(message)
    failure.url = url
    return failure


def _notice(failure: str, kind: str = "act") -> dict[str, Any]:
    """A ``web_search`` result that carries Grounder's message as its one row, in place of a failure Hermes would serve from its keyless ring.

    Hermes (0.23) serves any failed search of a non-ring backend from a free ring of other vendors' results, which hid a free-search limit's sign-up link
    and a rejected key behind unrelated rows; a successful result is never served that way. ``kind`` is ``act`` (the user must do something first, so
    ``after_tool`` has Hermes forget the result, which it would otherwise serve again for twenty minutes) or ``refused`` (this call is not made at all).
    """
    text = (f"Grounder did not run this search. {failure} Tell the user this; the same search fails again until they have." if kind == "act" else str(failure))
    url = getattr(failure, "url", "") or (GROUNDER_SITE if kind == "act" else "")
    return {"success": True, "data": {"web": [{"title": "Grounder", "url": url, "description": text, "position": 1}], NOTICE_KEY: kind}}


def _notice_pages(urls: list[str], failure: str) -> list[dict[str, Any]]:
    """The entries of a ``web_extract`` for a failure the user must act on: the message as each page's content, with no ``error``, so Hermes does not serve the
    pages from its keyless ring. Each is filed under Grounder's own address, not the page's, so Hermes caches none of them as the page's text."""
    url = getattr(failure, "url", "") or GROUNDER_SITE
    text = f"Grounder did not read this page. {failure} Tell the user this; the same read fails again until they have."
    return [{"url": url, "title": "Grounder", "content": text, "raw_content": text} for _ in urls]


def _description(result: dict[str, Any]) -> str:
    """The result's summary, else its text, on one line.

    Exactly what the provider returned: nothing is cut, chosen or reranked here. White space is normalised to single spaces, which loses no word.
    """
    return " ".join(str(result.get("summary") or result.get("text") or "").split())


# The conversation and turn of each live session, as Hermes's ``pre_tool_call`` hook last named them.
_TURNS: dict[str, tuple[str, str]] = {}
_TURNS_KEPT = 64
# Makes the tokens below random to everyone: they cannot be worked back to a Hermes id, and last only as long as this process.
_SALT = secrets.token_bytes(16)


def note_turn(**ids: Any) -> None:
    """``pre_tool_call`` hook: remember which conversation and turn this session is in. Changes nothing about the call. (Hermes applies a ``web_extract``'s
    ``char_limit`` to the whole page itself, so the plugin keeps none.)"""
    session = str(ids.get("session_id") or "")
    if not session:
        return
    _TURNS.pop(session, None)
    _TURNS[session] = (str(ids.get("conversation_id") or session), str(ids.get("turn_id") or ""))
    while len(_TURNS) > _TURNS_KEPT:
        _TURNS.pop(next(iter(_TURNS)), None)


# The model each live session last called, as Hermes's ``post_api_request`` hook named it: the model whose answer asked for the search that follows.
_MODELS: dict[str, str] = {}


def note_model(**request: Any) -> None:
    """``post_api_request`` hook: remember which model this session's last answer came from. Changes nothing about the call."""
    session = str(request.get("session_id") or "")
    model = str(request.get("response_model") or request.get("model") or "").strip()
    if not session or not model:
        return
    _MODELS.pop(session, None)
    _MODELS[session] = model[:80]
    while len(_MODELS) > _TURNS_KEPT:
        _MODELS.pop(next(iter(_MODELS)), None)


# The latest message the user sent in each live session, as Hermes's ``pre_llm_call`` hook gave it: the question a page no search found is read for
# (GRO-672, Harlan, 2026-11-10: "send the message"). A page read with no question comes back from its start, and on a long page the answer is further down.
_ASKS: dict[str, str] = {}


def _message_text(message: Any) -> str:
    """The words of a user message: a string, or the text parts of a list of content parts."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(str(part.get("text") or "") for part in message if isinstance(part, dict) and part.get("type") == "text")
    return ""


def note_message(**call: Any) -> None:
    """``pre_llm_call`` hook: remember the user's latest message in this session. Injects nothing and changes nothing about the call."""
    session = str(call.get("session_id") or "")
    text = " ".join(_message_text(call.get("user_message")).split())[:MAX_QUERY_CHARS]
    if not session or not text:
        return
    _ASKS.pop(session, None)
    _ASKS[session] = text
    while len(_ASKS) > _TURNS_KEPT:
        _ASKS.pop(next(iter(_ASKS)), None)


def _session() -> str:
    """The Hermes session this call runs in, or "" outside one."""
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_ID", "")
    except ImportError:
        return os.environ.get("HERMES_SESSION_ID", "")


def _chat(session: str) -> str:
    """The chat a session belongs to: the conversation Hermes's ``pre_tool_call`` last named for it, else the session. What the plugin remembers of the pages
    a search found is kept per chat: a gateway runs every user's sessions in one process, and one user's words and results
    must never reach another's."""
    return _TURNS.get(session, (session, ""))[0] if session else ""


def _token(*parts: str) -> str:
    return hashlib.sha256(_SALT + "\0".join(parts).encode()).hexdigest()[:32]


def _context() -> dict[str, Any]:
    """A random token for this session and one for this user prompt, so Grounder can tell one prompt from the next, and the model that asked for the search.
    Neither token carries a Hermes id or a message."""
    session = _session()
    if not session:
        return {}
    conversation, turn = _TURNS.get(session, (session, ""))
    model = _MODELS.get(session)
    return {"session_ref": _token(conversation), **({"prompt_ref": _token(conversation, turn)} if turn else {}), **({"agent": {"model": model}} if model else {})}


FREE_REFUSES = ("profile", "routing_settings", "workspace")  # what Grounder refuses a caller with no account (400): they change a search on an account's own settings


def _merged(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` laid on ``base``, setting by setting: where both say something for the same setting, ``over`` wins, and what only one says stays."""
    return {key: _merged(base[key], value) if isinstance(base.get(key), dict) and isinstance(value, dict) else value for key, value in {**base, **over}.items()}


def _names_a_provider(request: dict[str, Any]) -> bool:
    """Whether a request names the provider to search with (``requested_provider``, or ``providers`` with an entry): what Grounder 0.9.0 reads as Grounder direct."""
    asked, listed = request.get("requested_provider"), request.get("providers")
    return (isinstance(asked, str) and bool(asked.strip())) or (isinstance(listed, list) and bool(listed))


def _mode_of(request: dict[str, Any]) -> str:
    """The Grounder mode a request is: ``direct`` when its routing settings are exclusive or it names a provider (``_names_a_provider``), else ``preset`` when it names a
    ``profile`` other than ``auto``, else ``optimized``.

    Grounder 0.9.0 answers 400 when ``mode`` disagrees with the rest of the request, so ``direct`` goes first and ``_send`` drops the ``profile`` of a direct request:
    the user's direct choice wins over a preset, whether the preset is the user's saved one or the model's.
    """
    profile = request.get("profile")
    routing = request.get("routing_settings")
    if (isinstance(routing, dict) and routing.get("exclusive") is True) or _names_a_provider(request):
        return "direct"
    return "preset" if isinstance(profile, str) and profile.strip() not in ("", "auto") else "optimized"


def _send(payload: dict[str, Any], extra: Optional[dict[str, Any]] = None, dropped: Optional[list[str]] = None,
          via_out: Optional[list[str]] = None, sent_out: Optional[list[dict[str, Any]]] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One POST to Grounder: ``(body, None)``, or ``(None, error)`` when it failed. Never raises, and never retries: a second POST is a second search.

    ``extra`` is what only a signed-in search carries, the user's chosen preset, provider, mode and live-data APIs turned off (``chosen``): it goes with a search key or a
    sign-in, under anything the request itself already says, except that ``routing_settings`` are merged setting by setting with the user's winning where both name the same
    provider or live-data API (an API the user turned off stays off whatever the model passes), and never with a free search, which Grounder refuses a caller with no account. A user with neither a key nor a grant searches free,
    and a free search sends none of ``FREE_REFUSES`` either, which the model may have chosen: the names it asked for are added to ``dropped``, so the result can say so.
    A search that carries ``extra`` (a key or a sign-in with the user's choices, even none) names its Grounder mode in ``mode`` once those are merged in
    (``_mode_of``), unless the caller already set one, and a direct search drops any ``profile``; a free search and a page read, which carry no ``extra``, send neither.
    A Grounder from before 0.9.0 ignores ``mode``, and maps the same fields itself.
    An error the user must act on comes back as an ``_Act``. ``via_out`` gets how it was sent (``key``, ``sign-in`` or ``free``), so a caller knows whether ``extra`` went,
    and ``sent_out`` the request itself, the last entry being the one Grounder answered.
    """
    key = _key()
    context = _context()
    if context:
        payload = {**payload, "context": {**payload.get("context", {}), **context}}
    signed = {**{name: value for name, value in (extra or {}).items() if name not in payload}, **payload}
    if isinstance((extra or {}).get("routing_settings"), dict) and isinstance(payload.get("routing_settings"), dict):
        signed["routing_settings"] = _merged(payload["routing_settings"], extra["routing_settings"])  # the user's choice wins where both name the same provider or API
    if extra is not None and "mode" not in signed:
        signed["mode"] = _mode_of(signed)
        if signed["mode"] == "direct":
            signed.pop("profile", None)  # a direct search has no preset: Grounder 0.9.0 refuses the two together
    via = "key" if key else "sign-in"
    if via_out is not None:
        via_out.append(via)
    try:
        if key:
            if sent_out is not None:
                sent_out.append(signed)
            status, raw = _post(signed, key)
        else:
            try:
                if sent_out is not None:
                    sent_out.append(signed)
                status, raw = _post_signed_in(signed)
            except ProviderNotSignedIn:
                via = "free"
                if via_out is not None:
                    via_out.append(via)
                if dropped is not None:
                    dropped.extend(name for name in FREE_REFUSES if name in payload)
                free_request = {name: value for name, value in payload.items() if name not in FREE_REFUSES}
                if sent_out is not None:
                    sent_out.append(free_request)
                status, raw = _post(free_request)
    except AuthError as error:
        message = f"Grounder sign-in failed: {error}"
        # A refresh that failed on the network or Grounder's token service passes; any other sign-in failure is the user's to fix.
        return None, message if getattr(error, "code", None) == "oauth_refresh_failed" else _act(message)
    except PermissionError as error:
        return None, _act(f"Hermes refused the Grounder sign-in: {error}")
    except (httpx.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("Grounder request error: %s", error)
        return None, f"Could not reach Grounder: {error}"
    try:
        body = json.loads(raw)
    except ValueError:
        if status == 200:
            return None, "Could not parse the Grounder response as JSON"
        body = None
    if status != 200 or not isinstance(body, dict):
        message = _error_message(status, body, via)
        if not _must_act(status, body, via):
            return None, message
        link = str(_error_parts(body)[2].get("signup_url") or "") if via == "free" else ""
        return None, _act(message, link if link.startswith("https://") else "")
    return body, None


def _send_chosen(payload: dict[str, Any], dropped: Optional[list[str]] = None,
                 sent_out: Optional[list[dict[str, Any]]] = None) -> tuple[Optional[dict[str, Any]], Optional[str], list[str]]:
    """``_send`` with the user's choices (``chosen``). When Grounder refuses a choice it no longer offers (a 400: a removed preset, provider, mode or live-data API, refused
    before anything is read, called or spent: workers/router/src/lib/search.ts, ``resolveProfile`` and ``applyCallSettings`` checked over the defaults), drop that choice, so
    the user is not left with every search failing until they run ``/search`` again, and send once more without it. ``(body, error, what was dropped)``, one entry for each.

    A Grounder from before 0.8.16 refuses ``exclusive`` (a 400 naming it, before anything is called). A pinned provider is then sent once in the older form, always included,
    and ``OLDER_API`` is added to what comes back, so the result says the search may have included other providers."""
    gone: list[str] = []
    plain = False
    for _ in range(MAX_FORGET + 2):
        how: list[str] = []
        body, error = _send(payload, chosen(exclusive=not plain), dropped, how, sent_out)
        # A free search sent none of the user's choices, so a 400 cannot be about one of them.
        signed = body is None and not isinstance(error, _Act) and how[-1:] != ["free"]
        if signed and not plain and _predates_exclusive(str(error or "")):
            plain = True
            continue
        found = _stale_choice(str(error or ""), settings(), payload) if signed else None
        if found is None or len(gone) >= MAX_FORGET:
            return body, error, gone + ([OLDER_API] if plain and body is not None else [])
        changes, what = found
        try:
            save_settings(**changes)
        except Exception as problem:  # a config that cannot be written leaves the choice, and the 400 is said as it is
            logger.warning("Could not drop a choice Grounder no longer offers: %s", problem)
            return body, error, gone + ([OLDER_API] if plain and body is not None else [])
        gone.append(what)
    return body, error, gone + ([OLDER_API] if plain and body is not None else [])


OLDER_API = "only-this-provider"  # in what _send_chosen returns: this Grounder predates ``exclusive``, so the pin was sent as always included
_EXCLUSIVE_REFUSED = re.compile(r"Unknown setting: exclusive")  # what a Grounder from before 0.8.16 says of it (workers/router/src/lib/routing-settings.ts ``applySettings``)


def _predates_exclusive(error: str) -> bool:
    """Whether Grounder's 400 says it does not know ``exclusive``: an API from before 0.8.16."""
    return error.startswith("Grounder returned HTTP 400") and bool(_EXCLUSIVE_REFUSED.search(error))


def _dropped_notice(gone: list[str]) -> str:
    """What to tell the user, in the result, about the choices dropped for this search and a pin this Grounder could not make exclusive."""
    dropped = [name for name in gone if name != OLDER_API]
    notes = []
    if dropped:
        notes.append("Grounder no longer offers " + " or ".join(dropped) + ", so Hermes dropped it from the user's /search choices and searched without it. "
                     "Tell the user this, and that /search chooses again.")
    if OLDER_API in gone:
        notes.append("This Grounder cannot yet search with only the provider the user chose, so that provider was included and Grounder may have added others. "
                     "Tell the user this.")
    return " ".join(notes)


def _trim_address(address: str) -> str:
    """An address as it is written in running text: without the punctuation that ends a sentence, and with a closing bracket only when it closes one inside
    the address (``.../Python_(programming_language)`` keeps its ``)``; ``(see https://example.com/a)`` loses the one that is not the address's)."""
    opens = {")": "(", "]": "[", "}": "{"}
    while address:
        last = address[-1]
        if last in ".,;:!?" or (last in opens and address.count(last) > address.count(opens[last])):
            address = address[:-1]
        else:
            break
    return address


SECRET_REFUSED = ("Blocked: an address in this call carries what looks like an API key or token, and secrets must not be sent in addresses. "
                  "Nothing was sent to Grounder; leave that address out.")
PRIVATE_REFUSED = ("Blocked: an address in this call is not a public web address (a private, loopback or link-local one, or one that does not resolve), "
                   "so Grounder cannot read it. Nothing was sent to Grounder; leave that address out.")
# The shapes of a key: Grounder's own, always; and the well-known prefixes, for a Hermes with no ``agent.redact`` (Hermes's own list is used when it has one).
_OWN_KEY = re.compile(r"(?<![A-Za-z0-9])gr-[A-Za-z0-9]{24,}")
_KEY_SHAPES = re.compile(r"(?<![A-Za-z0-9])(?:sk-|sk_live_|rk_live_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abprs]-|AKIA|ASIA|AIza)[A-Za-z0-9_\-]{20,}")


def _private_host(host: str) -> bool:
    """Whether a host name is a private, loopback, link-local or otherwise non-public address, or a name only a local network has."""
    host = host.lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")) or "." not in host and ":" not in host:
        return True
    try:
        ip: Any = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))  # 2130706433, 0x7f.1 and 127.1 are all 127.x
        except OSError:
            return False
    return not (getattr(ip, "ipv4_mapped", None) or ip).is_global  # ::ffff:127.0.0.1 is read as the 127.0.0.1 inside it


def _refusal(address: str) -> str:
    """Why an address must not leave this machine for Grounder's reader, or "" when it may.

    Hermes refuses a ``web_extract`` of an address that carries a key (``agent.redact``'s prefixes, checked on the address and its percent-decoded form) and one
    that is not a public address (``tools.url_safety``). The same address as a ``web_search`` query or a ``grounder_search`` ``urls`` is read by Grounder's
    reader just the same, so each goes through the same checks first. Hermes's own are used when this Hermes has them; a host with a password in it, a literal
    private address, ``localhost`` and a local-only name are refused here either way. The reason never repeats the address.
    """
    forms = [address, unquote(address)]
    try:
        from tools.url_safety import normalize_url_for_request
        normalized = normalize_url_for_request(address)
        forms += [normalized, unquote(normalized)]
    except Exception:  # a Hermes without it
        pass
    try:
        from agent.redact import _PREFIX_RE as shapes
    except ImportError:
        shapes = _KEY_SHAPES
    own = _key()
    if any(shapes.search(form) or _OWN_KEY.search(form) or (own and own in form) for form in forms):
        return SECRET_REFUSED
    try:
        parts = urlsplit(address)
        if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password or _private_host(parts.hostname or ""):
            return PRIVATE_REFUSED
    except ValueError:
        return PRIVATE_REFUSED
    try:
        from tools.url_safety import is_safe_url
    except ImportError:  # a Hermes without it
        return ""
    return "" if is_safe_url(address) else PRIVATE_REFUSED


def _forget(args: Any) -> None:
    """Have Hermes drop the result it just cached for this ``web_search``. It caches every successful result for twenty minutes by query, and a notice that
    asks the user to act is one: without this a retry once they have signed in or added a key would be answered with the notice again. A Hermes that keeps
    no such cache, or keeps it another way, leaves it to expire."""
    try:
        from tools.web_result_cache import search_memo
        args = args if isinstance(args, dict) else {}
        key = search_memo._key("grounder", str(args.get("query") or ""), int(args.get("limit") or 5))
        with search_memo._store_lock:
            search_memo._store.pop(key, None)
    except Exception as error:
        logger.debug("Could not drop a Grounder notice from Hermes's cache: %s", type(error).__name__)


def _page_key(url: str, with_query: bool = True) -> str:
    """A page's address as Grounder compares addresses (no scheme, ``www.`` or fragment), so a result finds the page that was asked for."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower().rstrip(".")
    for prefix in ("www.", "m.", "amp.", "mobile."):
        if host.startswith(prefix) and len(host) > len(prefix) + 3:
            host = host[len(prefix):]
            break
    path = re.sub(r"/amp/?$", "", re.sub(r"/{2,}", "/", parts.path), flags=re.I).rstrip("/")
    query = "&".join(sorted(parts.query.split("&"))) if with_query and parts.query else ""
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _addresses(pages: list[str]) -> str:
    """The pages' addresses as a query, which asks nothing of them; only whole addresses, so that it stays a list of addresses within Grounder's limit."""
    query = " ".join(pages)
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS]
        query = query.rsplit(" ", 1)[0] if " " in query else query
    return query


def _pages_found(pages: list[str], results: list[Any]) -> dict[str, dict[str, Any]]:
    """Each requested page's result, by address. A result that matches no page is left out, and one with no text is a page not read."""
    rows = [r for r in results if isinstance(r, dict) and str(r.get("text") or "").strip()]
    exact: dict[str, dict[str, Any]] = {}
    for row in rows:
        exact.setdefault(_page_key(str(row.get("url", ""))), row)
    found = {page: exact[_page_key(page)] for page in pages if _page_key(page) in exact}
    # Grounder drops tracking parameters from the addresses it returns, so a page still unmatched is compared without its query.
    spare = [row for row in rows if not any(row is match for match in found.values())]
    for page in pages:
        if page not in found:
            loose = _page_key(page, with_query=False)
            row = next((r for r in spare if _page_key(str(r.get("url", "")), with_query=False) == loose), None)
            if row is not None:
                found[page] = row
                spare = [r for r in spare if r is not row]
    return found


# Why Grounder could not read a page, as the API's `unread` says (GRO-687): a model told "no such page" looks for the right address instead of guessing.
UNREAD = {
    "not_found": "Grounder could not read this page: it does not exist (the site answers 404). Search for its right address.",
    "refused": "Grounder could not read this page: its site does not allow readers.",
    "timed_out": "Grounder could not read this page in time: the site was too slow. Try again.",
}
NOT_READ = "Grounder could not read this page, or its site does not allow it."


def _unread(body: dict[str, Any]) -> dict[str, str]:
    """Each page the API says it could not read, by address as compared, with why."""
    pages = body.get("unread") if isinstance(body.get("unread"), list) else []
    return {_page_key(str(page.get("url", ""))): str(page.get("reason", "")) for page in pages if isinstance(page, dict)}


def _page_row(url: str, row: Optional[dict[str, Any]], reason: str = "") -> dict[str, Any]:
    """Hermes's entry for one page, or its error when no result read it, saying why when the API knows. The page's text is as Grounder returned it, stripped."""
    if row is None:
        return _page_error(url, UNREAD.get(reason, NOT_READ))
    text = str(row.get("text") or "").strip()
    return {"url": url, "title": str(row.get("title", "")), "content": text, "raw_content": text}


def _page_error(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error}


def _page_shown(url: str, shown: tuple[str, str]) -> dict[str, Any]:
    """Hermes's entry for a page Grounder could not read but a search returned: what that search showed of it, which says so (GRO-694).
    No ``error``, so Hermes keeps it as read and its own reader does not fetch the page again."""
    title, text = shown
    content = f"{SHOWN_NOTE}\n\n{text}"
    return {"url": url, "title": title, "content": content, "raw_content": content}


def _options(args: dict[str, Any], query: str) -> dict[str, Any]:
    """``grounder_search``'s arguments as a search request, the way Grounder's MCP search maps the same arguments."""
    payload: dict[str, Any] = {"query": query}
    for name in ("top_k", "include_text", "requested_provider", "providers", "features", "urls", "routing_settings", "workspace", "profile"):
        if args.get(name) not in (None, "", [], {}):
            payload[name] = args[name]
    filters: dict[str, Any] = {}
    if args.get("domains"):
        filters["domains"] = {"values": args["domains"], "mode": "include"}
    if isinstance(args.get("days"), int) and args["days"] > 0:
        filters["recency"] = {"mode": "rolling", "days": args["days"]}
    if filters:
        payload["filters"] = filters
    stated = {name: str(args[name]).strip()[:limit] for name, limit in (("user_location", 200), ("user_time_zone", 64)) if str(args.get(name) or "").strip()}
    if stated:
        payload["context"] = stated
    return payload


def _feature(feature: dict[str, Any]) -> dict[str, Any]:
    """One feature as Grounder's MCP ``list_providers`` lays it out: the providers with their modes, and the live-data APIs."""
    providers: dict[str, list[str]] = {}
    for offering in feature.get("offerings") or []:
        modes = providers.setdefault(str(offering.get("provider")), [])
        if offering.get("mode"):
            modes.append(str(offering["mode"]))
    return {"feature": feature.get("id"), "description": feature.get("description"), "returns": feature.get("returns"),
            "inputs": feature.get("inputs"), "providers": providers, "apis": feature.get("specialized") or []}


def search_modes() -> list[dict[str, str]]:
    """The presets Grounder lists now, as ``[{"id", "name", "description"}]``; empty when the list cannot be read, which leaves the rest of an answer as it was."""
    try:
        status, raw = _get(PROFILES_URL)
        rows = json.loads(raw).get("profiles") if status == 200 else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError):
        return []
    return [{"id": str(row["id"]), "name": str(row.get("name") or row["id"]), "description": str(row.get("description") or "")}
            for row in rows if isinstance(row, dict) and row.get("id")] if isinstance(rows, list) else []


class GrounderWebSearchProvider(WebSearchProvider):
    """Grounder's search API as a Hermes web search and extract provider."""

    # Where Hermes honours it (newer Hermes, and the Grounder provider it bundles), a call that failed here is not served from Hermes's keyless ring of other
    # vendors, so Grounder's own message reaches the user. Hermes 0.23 reads only the config key ``web.keyless_rescue`` and ignores this: there, a failure the
    # user must act on comes back as a result instead (``_notice``), and only a transient one (a timeout, a network error, another 5xx) is left to the ring.
    keyless_rescue = False

    def __init__(self) -> None:
        # Each page a search returned and that search's query, oldest first, so an extract can be ranked against it, by chat and page: another user's session
        # in the same process must not read them. In memory only.
        self._found_by: dict[tuple[str, str], str] = {}
        # Each such page's title and the text the search showed of it, for a read of it that fails. In memory only.
        self._shown: dict[tuple[str, str], tuple[str, str]] = {}
        # The pages Grounder read. In memory only.
        self._read: dict[tuple[str, str], bool] = {}
        # Each search's summary by its query: how the hooks tell Grounder's results from another backend's, and what Hermes's own cache serves again. It holds
        # providers, counts and times, never words of a result, so a search Hermes answers from its cache for another session still gets its line.
        self._searched: dict[str, dict[str, Any]] = {}
        # The searches of each turn not yet answered, by session and turn, and the turns whose model has read the notes.
        self._turns: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._noted: dict[tuple[str, str], bool] = {}
        # Each turn's results Grounder served, by page, with the number the model cites it by and its title and link. An answer lists only its own turn's.
        self._sources: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "grounder"

    @property
    def display_name(self) -> str:
        return "Grounder"

    def is_available(self) -> bool:
        """Always true: a search needs no account, so Grounder is ready with no key and no sign-in.

        Hermes turns web_search and web_extract off when no selected backend says it is ready. With neither a key nor a grant a search is a free search, so
        the backend is ready; the sign-in client the plugin writes at its first load only saves the user a step when they do sign in.
        """
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Search Grounder for up to ``limit`` results (1 to ``MAX_RESULTS``), or read the pages a query names (``_read_part``)."""
        addresses = [address for address in dict.fromkeys(_trim_address(found) for found in ADDRESS.findall(query)) if address]
        if addresses:
            # Every address in the query is checked before any is read: one that carries a key, or is not public, ends the call and nothing is sent.
            why = next((reason for address in addresses if (reason := _refusal(address))), "")
            return _notice(why, "refused") if why else self._read_part(query, addresses[:MAX_PAGES])
        # No text_max_chars, passage or rerank option: each result's text is what the provider returned. Rows are cut only to the limit Hermes asked for.
        payload = {"query": query[:MAX_QUERY_CHARS],
                   "top_k": max(1, min(int(limit), MAX_RESULTS))}
        sent: list[dict[str, Any]] = []
        body, error, gone = _send_chosen(payload, sent_out=sent)
        if body is None:
            return _notice(error) if isinstance(error, _Act) else _fail(error or "Grounder returned no answer")
        rows = [_row(r, i + 1) for i, r in enumerate(_results(body)[:max(1, int(limit))])]
        self._remember(query[:MAX_QUERY_CHARS], rows, _summary(body, len(rows), sent[-1] if sent else None))
        # The count only: a query is the user's words, and a log line outlives the search.
        logger.info("Grounder search: %d results", len(rows))
        return {"success": True, "data": {"web": rows, **({NOTE_KEY: _dropped_notice(gone)} if gone else {})}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Read ``urls`` in one search, so it counts once: an entry per URL in their order, with an ``error`` on each page not read.

        Each page comes back as Grounder returned it: no ``text_max_chars`` is sent and nothing is cut here (Hermes applies a model's ``char_limit`` to the whole page
        itself). Hermes's ``format`` and other ``kwargs`` change nothing. An address that
        carries a key or is not public is not sent (Hermes refuses most before this point): its entry says so, and the others are read.
        """
        asked = [str(url) for url in urls]
        pages = list(dict.fromkeys(asked))[:MAX_PAGES]
        if not pages:
            return []
        refused = {page: reason for page in pages if (reason := _refusal(page))}
        pages = [page for page in pages if page not in refused]
        if not pages:
            return [_page_error(url, refused[url]) for url in asked]
        chat = _chat(_session())
        body, error = _send({"query": self._question(pages), "urls": pages, "top_k": len(pages), "features": ["page-text"]})
        if body is None:
            failed = _notice_pages(asked, error) if isinstance(error, _Act) else [_page_error(url, error or "Grounder returned no answer") for url in asked]
            return [_page_error(url, refused[url]) if url in refused else entry for url, entry in zip(asked, failed)]
        found = _pages_found(pages, _results(body))
        # The count only: an address says what the user is reading, and a log line outlives the search.
        logger.info("Grounder extract: %d of %d pages read", len(found), len(pages))
        with self._lock:
            shown = {page: self._shown[(chat, _page_key(page))] for page in pages if page not in found and (chat, _page_key(page)) in self._shown}
            for page in [*found, *shown]:
                self._read[(chat, _page_key(page))] = True
            _bounded(self._read, REMEMBERED_PAGES)
        why = _unread(body)
        return [_page_error(url, refused[url]) if url in refused else
                _page_shown(url, shown[url]) if url in shown else _page_row(url, found.get(url), why.get(_page_key(url), "")) for url in asked]

    def _read_part(self, query: str, pages: list[str]) -> dict[str, Any]:
        """A ``web_search`` whose query holds pages' addresses: read those pages for the query's other words, in ``web_search``'s shape (GRO-691).

        Hermes's ``web_extract`` takes only addresses, and answers a repeated read from its own cache, so a model that needed another part of a page
        fetched it with its own tools. Hermes keys its search cache by the query, so a new question reaches Grounder. A query of addresses alone is
        ranked as an extract is, against the search that found the pages. Each page comes back whole, as Grounder returned it: no ``text_max_chars`` is sent.
        """
        chat = _chat(_session())
        question = " ".join(ADDRESS.sub(" ", query).split())[:MAX_QUERY_CHARS] or self._question(pages)
        sent: list[dict[str, Any]] = []
        body, error = _send({"query": question, "urls": pages, "top_k": len(pages), "features": ["page-text"]}, sent_out=sent)
        if body is None:
            return _notice(error) if isinstance(error, _Act) else _fail(error or "Grounder returned no answer")
        found = _pages_found(pages, _results(body))
        logger.info("Grounder read by search: %d of %d pages read", len(found), len(pages))
        with self._lock:
            shown = {page: self._shown[(chat, _page_key(page))] for page in pages if page not in found and (chat, _page_key(page)) in self._shown}
            for page in [*found, *shown]:
                self._read[(chat, _page_key(page))] = True
            _bounded(self._read, REMEMBERED_PAGES)
        if not found and not shown:
            why = _unread(body)
            reasons = {why.get(_page_key(page), "") for page in pages}
            if len(pages) == 1 or len(reasons) == 1:
                return _fail(UNREAD.get(reasons.pop(), NOT_READ) if len(pages) == 1 else UNREAD.get(reasons.pop(), NOT_READ).replace("this page", "these pages"))
            return _fail("Grounder could not read these pages, or their sites do not allow it.")
        rows = []
        for page in pages:
            if page in found or page in shown:
                entry = _page_row(page, found[page]) if page in found else _page_shown(page, shown[page])
                rows.append({"title": entry["title"], "url": page, "description": entry["content"], "position": len(rows) + 1})
        self._remember(query[:MAX_QUERY_CHARS], rows, _summary(body, len(rows), sent[-1] if sent else None), found_by=False)
        return {"success": True, "data": {"web": rows}}

    def after_tool(self, tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", turn_id: str = "", conversation_id: str = "",
                   **_: Any) -> Optional[str]:
        """Hermes's ``transform_tool_result``: note a search for the line under the answer, number each result Grounder served across the turn, and add
        the citation request to the turn's first one.

        Hermes keeps only a page's url, title, content and error from an extract, so its number and the note are added here, after that trim, and never
        inside a page's text. What the line under the answer counts is what the model was shown, which is this result: Hermes asks for more rows than a
        model's ``limit`` and cuts them to it before this hook sees them.
        """
        if tool_name not in ("web_search", SEARCH_TOOL, "web_extract"):
            return None
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        data = data if isinstance(data, dict) else {}
        if isinstance(data.get("data"), dict) and data["data"].get(NOTICE_KEY):
            # Grounder's message to the user, not a search: nothing in it is a source. Hermes cached it for twenty minutes, and a retry once the user has
            # acted must reach Grounder.
            if data["data"][NOTICE_KEY] == "act":
                _forget(args)
            return None
        turn = (session_id, turn_id)
        chat = conversation_id or _chat(session_id)
        # A host that names no turn cannot say which answer a result serves: positions stay the search's own, and every result gets the notes.
        named = all(turn)
        with self._lock:
            if tool_name == "web_extract":
                entries = data.get("results") if isinstance(data.get("results"), list) else []
                served = [entry for entry in entries if isinstance(entry, dict) and entry.get("content") and entry.get("url")
                          and (chat, _page_key(str(entry["url"]))) in self._read]
            else:
                summary = self._searched.get(str((args or {}).get("query", ""))[:MAX_QUERY_CHARS])
                rows = (data.get("data") or {}).get("web") if isinstance(data.get("data"), dict) else None
                served = [row for row in rows if isinstance(row, dict) and row.get("url")] if summary is not None and summary["sources"] and isinstance(rows, list) else []
                if served and summary is not None:
                    # A search is timed once, when it ran: the same query answered again from Hermes's cache took no time.
                    ran, summary["fresh"] = summary["fresh"], False
                    self._turns.setdefault(turn, []).append({"mode": summary["mode"], "via": summary["via"], "ms": summary["ms"] if ran else 0, "from": summary["from"],
                                                             "pages": {_page_key(str(row["url"])) for row in served}})
                    _bounded(self._turns, REMEMBERED_TURNS)
            if not served:
                return None
            if named:
                sources = self._sources.setdefault(turn, {})
                _bounded(self._sources, REMEMBERED_TURNS)
                for entry in served:
                    key = _page_key(str(entry["url"]))
                    if key not in sources:
                        sources[key] = {"n": len(sources) + 1, "title": " ".join(str(entry.get("title") or "").split()), "url": str(entry["url"])}
                    entry["position"] = sources[key]["n"]
            notes = not named or turn not in self._noted
            if named and notes:
                self._noted[turn] = True
                _bounded(self._noted, REMEMBERED_TURNS)
        extra = {CITE_KEY: CITE_REQUEST if named else CITE_REQUEST_LISTED} if notes else {}
        return json.dumps({**data, **extra}, indent=2, ensure_ascii=False)

    def after_answer(self, response_text: str = "", session_id: str = "", turn_id: str = "", **_: Any) -> Optional[str]:
        """Hermes's ``transform_llm_output``: end an answer with the Sources list of what it cites and, when it searched, Grounder's card line.
        The list names the results the answer cites that this turn's own searches and reads returned, and none from an earlier turn: a turn that searched
        nothing has no list, and its answer cites nothing by number. Appends only, so Hermes keeps streaming the answer."""
        with self._lock:
            searches = self._turns.pop((session_id, turn_id), None)
            sources = self._sources.pop((session_id, turn_id), None) or {}
        if not response_text:
            return None
        tail = [part for part in (_sources_list(response_text, sources), _card_line(searches) if searches else "") if part]
        return f"{response_text.rstrip()}\n\n" + "\n\n".join(tail) if tail else None

    def search_tool(self, args: dict[str, Any], **_: Any) -> str:
        """The ``grounder_search`` tool: one search with every option Grounder's MCP search takes, answered in ``web_search``'s shape."""
        query = str(args.get("query") or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return json.dumps(_fail("grounder_search needs a query"))
        asked = args.get("urls")
        why = next((reason for address in ([asked] if isinstance(asked, str) else asked if isinstance(asked, list) else []) if (reason := _refusal(str(address)))), "")
        if why:
            return json.dumps(_fail(why))
        # A page read is not shaped by a mode the user chose for searches, so it carries only what the model asked.
        dropped: list[str] = []
        sent: list[dict[str, Any]] = []
        if args.get("urls"):
            body, error = _send(_options(args, query), None, dropped, sent_out=sent)
            gone: list[str] = []
        else:
            body, error, gone = _send_chosen(_options(args, query), dropped, sent)
        if body is None:
            return json.dumps(_fail(error or "Grounder returned no answer"))
        rows = [_row(r, i + 1) for i, r in enumerate(_results(body))]
        self._remember(query, rows, _summary(body, len(rows), sent[-1] if sent else None))
        logger.info("Grounder search: %d results", len(rows))
        features = {"features": body["features"]} if body.get("features") else {}
        note = {"note": f"No Grounder account is connected, so {', '.join(dropped)} {'was' if len(dropped) == 1 else 'were'} not sent and this ran as a free search. "
                        f"Sign in with `{SIGN_IN}`, or set a search key in `{KEY_ENV}`, to use {'it' if len(dropped) == 1 else 'them'}."} if dropped else {}
        return json.dumps({"success": True, "data": {"web": rows, **({NOTE_KEY: _dropped_notice(gone)} if gone else {})}, **features, **note}, indent=2, ensure_ascii=False)

    def list_providers_tool(self, args: dict[str, Any], **_: Any) -> str:
        """The ``grounder_list_providers`` tool: what Grounder's MCP ``list_providers`` says, from the public feature list. Sends no credential and is not a search."""
        request = urllib.request.Request(FEATURES_URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with _open(request, TIMEOUT_S) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            return json.dumps(_fail(f"Could not reach Grounder: {error}"))
        data: dict[str, Any] = {"features": [_feature(f) for f in body.get("features") or [] if isinstance(f, dict)]}
        modes = search_modes()
        if modes:
            data["search_modes"] = modes
        return json.dumps({"success": True, "data": data}, ensure_ascii=False)

    def _remember(self, query: str, rows: list[dict[str, Any]], summary: dict[str, Any], found_by: bool = True) -> None:
        """Note the query that found each of a search's pages, for the extract that may read them next, and the search's summary.
        A read by search names its pages rather than finding them, so it leaves the query that found them in place."""
        chat = _chat(_session())
        with self._lock:
            for row in rows if found_by else []:
                key = (chat, _page_key(row["url"]))
                self._found_by.pop(key, None)  # a page found again moves to the newest place
                self._found_by[key] = query
                if row.get("description"):
                    self._shown.pop(key, None)
                    self._shown[key] = (row["title"], row["description"])
            _bounded(self._found_by, REMEMBERED_PAGES)
            _bounded(self._shown, REMEMBERED_PAGES)
            self._searched.pop(query, None)
            self._searched[query] = summary  # kept after the turn: Hermes may answer the same search from its own cache
            _bounded(self._searched, REMEMBERED_TURNS)

    def _question(self, pages: list[str]) -> str:
        """What to rank the pages against: the web_search of this chat that found the most of them, else the user's latest message, else their addresses, which
        ask nothing."""
        chat = _chat(_session())
        with self._lock:
            found = [self._found_by.get((chat, _page_key(page))) for page in pages]
        counts = Counter(query for query in found if query)
        if counts:
            return counts.most_common(1)[0][0]
        return _ASKS.get(_session()) or _addresses(pages)

    def get_setup_schema(self) -> dict[str, Any]:
        """The entry ``hermes tools`` shows. It asks for no key, since signing in needs none; the tag names both ways in."""
        return {
            "name": "Grounder", "badge": "account",
            "tag": "Licensed, live sources: over 40 providers, one call each, "
                   f"each returning its own results, and page reading. Free searches need no account; for more, a search key in `{KEY_ENV}`, or sign in with `{SIGN_IN}`.",
            "env_vars": [],
        }
