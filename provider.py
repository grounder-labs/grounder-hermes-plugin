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

A result Grounder served says so in one line (``served_by``: the mode, who answered, and how
many of its results the model is shown) and asks the model to cite what it uses by number, and an
answer ends with the Sources list of what it cites and, when it searched, the line
Grounder's card shows (``after_tool`` and ``after_answer``, Hermes's result and reply
hooks). Both work on what is already on this machine and send nothing. A
``web_search`` whose query holds a page's address reads that page for the rest of
the query, and the page comes back whole.

Grounder provider returns the results of the sources that answered. Grounder direct finds the pages to read from those sources.
A page named by address is a free read. The line under each answer uses the response's mode and sources.

The plugin selects itself the first time it loads in a profile (``__init__.py``), by
setting ``web.search_backend: grounder`` and ``web.extract_backend: grounder`` for each the user has set no backend for.
A call uses the Grounder search key in ``GROUNDER_API_KEY`` when one is set, and otherwise the sign-in Hermes keeps
for the ``oauth:`` client in ``plugin.yaml`` (``hermes auth add grounder:grounder``):
Hermes signs in, stores and refreshes that grant and attaches its token to the
request, so this plugin never holds a token. With neither, a search is one of Grounder's free searches: it carries no
credential. The local choice applies when there is no connected account. A free search that reaches Grounder's limit says where to sign up and add credit, with the links the API gave (a 402, or a 429 or 503 with those links): never a bare HTTP status.
With a wallet set (``GROUNDER_X402_PRIVATE_KEY``) a free search that Grounder answers 402 is paid for once with x402 and sent again (``_pay``, ``payment.py``);
what is paid, and what is never paid, is ``payment.choose``. No request the plugin makes follows
a redirect (``_open``), and no address that carries a key or is not public is sent (``_refusal``).

``/search`` saves the search choice and format on the Grounder account when connected, otherwise locally. The resolver is local.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
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
from hermes_cli.plugin_provider_requests import (
    ProviderNotSignedIn,
    credentialed_provider_request,
)

from . import direct_dev, direct_formats, payment

logger = logging.getLogger(__name__)

ORIGIN = "https://api.grounder.net"
SEARCH_URL = f"{ORIGIN}/v1/search"
SIGN_INS_URL = f"{ORIGIN}/v1/direct/sign-ins"  # where Grounder direct reports the sites it has a saved sign-in for (names, this Mac, when saved), with the key or the sign-in
SETTINGS_URL = f"{ORIGIN}/v1/search/settings"  # the account's saved search setting, read and written with a key or the sign-in; the Routing page on grounder.net shows the same one
OAUTH_ID = "grounder"  # the id of the oauth: entry in plugin.yaml
SIGN_IN = f"hermes auth add grounder:{OAUTH_ID}"  # the plugin's key (its name), a colon, the entry's id
USER_AGENT = "grounder-hermes/0.10.4"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
KEY_ENV = "GROUNDER_API_KEY"
BAD_KEY = (f"{KEY_ENV} holds a character no search key has (a space, a line break or a letter outside plain ASCII). "
           f"Set it again with `hermes config set {KEY_ENV} <key>`, or sign in with `{SIGN_IN}`.")
CLIENT_ENV = "GROUNDER_OAUTH_CLIENT_ID"  # filled in at the first load when there is no search key
CLIENT_ID = f"{ORIGIN}/oauth/clients/hermes-agent"  # public, the same for everyone
MAX_RESULTS = 100  # most results one search asks Grounder for: its top_k range, and the 100 Hermes clamps a model's limit to
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
MODES = ("grounder-provider", "grounder-direct")
PROVIDER_MODE = MODES[0]
DIRECT_MODE = direct_formats.DIRECT_MODE
MAX_PAGES = 5
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
                "every cited result's title and link under the answer, and one you write costs tokens for nothing. "
                "Grounder also adds one line under the answer that starts \"Searched with\": its own record of this answer's searches (mode, who answered, "
                "sources shown, time). Each result's `served_by` says the same of that search, so when asked whether Grounder was used, answer from them.")
# What a result Grounder served says of itself, in one line the model reads (GRO-918). Hermes marks only a search another backend rescued (`data.served_by`,
# `data.rescued_from`, `data.backend_error`), so a model shown a served result with no such field told its user Grounder "contributed nothing" and disowned the line
# under its own answer (Harlan, 2026-11-17: "big issue high priority"). The same key, one line a search, never a result's own provenance (GRO-698):
# `Grounder preset (Live) via You.com, ESPN; 5 of its 10 results shown`.
SERVED_KEY = "served_by"
# For a host that names no turn the plugin cannot tell which answer a result serves, so the model writes the list itself. The first sentence is word for
# word what Grounder's MCP server ends every search's notes with (CITE_REMINDER in routes/mcp.ts).
CITE_REQUEST_LISTED = ('Cite every result your answer uses by its "Cite as" line (its title, whose it is and its site, its date, then its link), in this answer and in every later one in this chat, not only the first. '
                       "End the answer with a Sources list: one line per result used, in that form: its title in quotes, its source, its date when it has one, "
                       "then its link. Never a bare link or a bare title.")
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
CODE = re.compile(r"(?s)(?:\x60{3}|~~~).*?(?:\x60{3}|~~~|\Z)|(\x60+)(?!\x60)(?:(?!\n\n).)+?(?<!\x60)\1(?!\x60)")  # \x60 is the backtick, spelled so no line reads as a shell snippet to `hermes plugins validate`
SEARCH_TOOL = "grounder_search"
TOOLSET = "grounder"
SEARCH_SCHEMA = {
    "name": SEARCH_TOOL,
    "description": (
        "Better web routing. Use for questions that need current or verifiable information that Grounder has a source for. "
        "Grounder provider returns the answering sources' results. Grounder direct finds their pages and reads them. "
        "Use urls when the user gives a page address, or site and query when they name a site without a page address. "
        "Reading a page named by address is free. " + direct_dev.SEARCH_PRICE
    ),
    "parameters": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "The question, as the user asked it."},
        "top_k": {"type": "integer", "description": "How many results (1-100). Default 10."},
        "days": {"type": "integer", "description": "Only results published within this many days, where the topic supports it."},
        "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_PAGES,
                 "description": "Addresses of pages the user asks to read with Grounder direct."},
        "include_text": {"type": "boolean", "description": "Include each result's text. Default true."},
        "user_location": {"type": "string", "description": "Where the user is, only when stated in this chat."},
        "user_time_zone": {"type": "string", "description": "The user's IANA time zone, only when stated in this chat."},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "Use grounder-direct when the user asks to read a page. Leave mode out to use the saved choice."},
        "site": {"type": "string", "description": "Only a host the user's own words write, such as bbc.co.uk, never one inferred from a name (the Oscars is not oscars.org). Put their words in query. " + direct_dev.SEARCH_PRICE},
        "signed_in": {"type": "boolean", "description": "Grounder direct only. Set false when the user asks to read logged out. Saved sign-ins stay on the Mac."},
        "format": {"type": "string", "enum": list(direct_formats.FORMATS),
                   "description": "Summary, full-page or highlights. Send only when the user's words name a format."},
    }},
}
# Where a provider's or live source's id is not its name capitalized; the rest of Grounder's lists are.
PROVIDER_NAMES = {"you": "You.com", "xai": "xAI", "newsapi": "NewsAPI.ai", "tinyfish": "TinyFish", "aeroapi": "FlightAware",
                  "ap-elections": "AP Elections", "bestbuy": "Best Buy", "espn": "ESPN", "fed": "Federal Reserve", "mlb": "MLB",
                  "nws": "National Weather Service", "odds-api": "The Odds API", "openf1": "OpenF1", "queue-times": "Queue-Times.com",
                  "seatgeek": "SeatGeek", "statuspage": "Status pages", "truth-social": "Truth Social", "usgs": "USGS",
                  "yahoo-finance": "Yahoo Finance", "youtube": "YouTube"}
# One search, across every provider it reaches: the longest a Grounder search can run, plus 10 s for the network and the answer (GRO-867). A search's budget is its longest
# lane, a first call and the one fallback behind it. A long research run takes 130 s (one you choose, as a saved or named provider: Grounder never picks one itself) and a deep
# fallback 45 s behind it: 175 s in all. A deep run and its deep fallback are 90 s, Valyu's 15 s search and its fallback 19 s, Tako's 6 s search and its fallback about 10 s.
# The Grounder service's own tests fail when its longest lane outgrows this figure.
TIMEOUT_S = 40
KEYS_URL = "https://grounder.net/account/keys"
SETTINGS_KEY = "grounder"  # the section of config.yaml's web: that holds the usage-context setting
SHARE_USAGE_KEY = "share_usage_context"  # in that section: whether a search carries the model that asked for it and a token for its session and prompt
SHARE_USAGE_DEFAULT = False  # what it is when the user has not set it: usage reporting is opt-in (Harlan, 2026-11-13, GRO-858); True would send it unless the user turns it off
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _bounded(store: dict, limit: int) -> None:
    """Drop the oldest entries of ``store`` past ``limit``."""
    while len(store) > limit:
        del store[next(iter(store))]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _duration(ms: int) -> str:
    """A search's time as Grounder's card writes it."""
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


# What each of `usage.steps` is called on the card line (GRO-719). The API's steps add up to its time with nothing counted twice, so the line names exactly the ones it was
# sent and no remainder of its own.
STEP_WORDS = {"choose": "choosing sources", "sources": "sources", "licence": "licence check", "pages": "page pick", "read": "page read", "answer": "answer"}


def _steps_of(usage: dict[str, Any]) -> list[tuple[str, int]]:
    """``usage.steps`` as ``(step, ms)`` pairs in the order sent: only the steps the plugin has a word for, with whole positive milliseconds."""
    steps = usage.get("steps")
    return [(str(one["step"]), one["latency_ms"]) for one in steps if isinstance(one, dict) and one.get("step") in STEP_WORDS
            and isinstance(one.get("latency_ms"), int) and not isinstance(one.get("latency_ms"), bool) and one["latency_ms"] > 0] if isinstance(steps, list) else []


def _provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider.capitalize())


def _fetched(result: dict[str, Any]) -> str:
    """When a result's text was fetched, as the row says it (GRO-939): ``during this search``, the day the provider's copy was fetched, or ``not given`` for a
    stored copy of unknown age; "" when the response carries no ``fetched`` (an older API), so the row says nothing rather than guess."""
    fetched = result.get("fetched")
    if not isinstance(fetched, dict):
        return ""
    if fetched.get("live") is True:
        return "during this search"
    at = fetched.get("at")
    return at[:10] if isinstance(at, str) and at.strip() else "not given"


def _credit(licence: Any) -> str:
    """The credit line a topic's source asks for (GRO-972), as the row says it: the line itself, and the licence's name when it has one; "" when the topic carries none (an older API's answer, or a source that asks for nothing)."""
    if not isinstance(licence, dict) or not isinstance(licence.get("credit"), str) or not licence["credit"].strip():
        return ""
    credit, name = licence["credit"].strip(), licence.get("name")
    return f"{credit} ({name.strip()})" if isinstance(name, str) and name.strip() and name.strip() not in credit else credit


def _row(result: dict[str, Any], position: int) -> dict[str, Any]:
    """Hermes's row for one result, with its date when Grounder has one and when its text was fetched (GRO-939), so a stored copy is not read as live.

    A result's provenance (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's records and in the API's
    response, and the line under an answer names the providers; the model reads none of it per result (GRO-698: about 100 characters a result).
    """
    date = str(result.get("published_date") or "")[:10]
    fetched = _fetched(result)
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    credit = _credit(provenance.get("licence"))
    return {"title": str(result.get("title") or ""), "url": str(result.get("url") or ""),
            **{key: provenance[key] for key in ("topic", "source") if provenance.get(key)},
            **({"licence": credit} if credit else {}),
            "description": _description(result), "position": position, **({"date": date} if date else {}), **({"fetched": fetched} if fetched else {})}


def _listed(value: Any) -> list[Any]:
    """``value`` when it is a list, else none: a response whose list is another shape is read as having none."""
    return value if isinstance(value, list) else []


def _candidates(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Use only addresses the server selected for the Mac reader. Feed rows are results, not pages to read."""
    direct = body.get("direct") if isinstance(body.get("direct"), dict) else {}
    return [row for row in _listed(direct.get("candidates")) if isinstance(row, dict)]


def _results(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The result rows of a response: each one that is an object with an address (every result has one). A null among them, a row with none, or a ``results``
    that is not a list, never fails the search."""
    return [result for result in _listed(body.get("results")) if isinstance(result, dict) and isinstance(result.get("url"), str) and result["url"].strip()]


# Where this process reads the time of a search, so the time under an answer can be tested.
_clock = time.monotonic


def _summary(body: dict[str, Any], sources: int, sent: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the line under an answer needs of one search."""
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    mode = routing.get("mode")
    if mode not in MODES:
        mode = (sent or {}).get("mode")
    way = "Grounder direct" if mode == DIRECT_MODE else "Grounder provider"
    topics = [topic for topic in _listed(body.get("topics")) if isinstance(topic, dict)]
    via = list(dict.fromkeys(str(topic["source"]) for topic in topics if topic.get("source") and topic.get("reached") != "none" and topic.get("status", "ok") == "ok"))
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    latency = usage.get("latency_ms")
    ms = latency if isinstance(latency, int) and not isinstance(latency, bool) else 0
    return {"mode": way, "via": via, "sources": sources, "ms": ms, "steps": _steps_of(usage),
            "from": _clock() - ms / 1000, "fresh": True}


def _served_line(summary: dict[str, Any], shown: int) -> str:
    """``SERVED_KEY``'s line for one search Grounder served: the mode it ran in, who answered, and how many of the results Grounder returned the model is shown when
    Hermes cut them to the model's ``limit``. With no mode known it says only Grounder."""
    line = (summary.get("mode") or "Grounder") + (" via " + ", ".join(summary["via"]) if summary.get("via") else "")
    found = summary.get("sources") or 0
    return line + (f"; {shown} of its {found} results shown" if shown < found else "")


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
        # The steps are one search's own clock: two searches of an answer may have run together, so only a lone search says where its time went.
        steps = searches[0].get("steps") if len(searches) == 1 else None
        parts.append(_duration(total) + (f" ({', '.join(f'{STEP_WORDS[step]} {_duration(ms)}' for step, ms in steps)})" if steps else ""))
    return " · ".join(parts)


_DIRECT_HEADER = re.compile(r"\[Grounder direct: read directly from ([^\s(,]+).*?HTTP \d+, (\d+) ms", re.S)
_FORMAT_LINE = re.compile(r"\nFormat: ([a-z-]+)\.")


def _fetched_line(fetched: list[dict[str, Any]]) -> str:
    """The line under an answer for the pages Grounder fetched straight from their sites or extracted with its reader: ``Grounder fetched www.cnn.com
    (summary) · 9.0 s`` and ``Grounder extracted example.com``."""
    lines = []
    for verb in ("fetched", "extracted"):
        rows = [row for row in fetched if row["verb"] == verb]
        if not rows:
            continue
        hosts = list(dict.fromkeys(row["host"] for row in rows))
        formats = list(dict.fromkeys(row["format"] for row in rows if row["format"]))
        parts = [f"Grounder {verb} " + ", ".join(hosts[:3]) + (f" +{len(hosts) - 3}" if len(hosts) > 3 else "") + (f" ({', '.join(formats)})" if formats else "")]
        if len(rows) > 1:
            parts.append(_plural(len(rows), "page"))
        total = sum(row["ms"] for row in rows)
        if total:
            parts.append(_duration(total))
        lines.append(" · ".join(parts))
    return "\n".join(lines)


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


def _citation(source: dict[str, Any], link: str) -> str:
    """One source cited in full: its title in quotes, whose it is (the source's name, else its site), its date when it has one, then its link (GRO-985)."""
    title = _plain(source.get("title") or "").rstrip(". ")
    named = f"\"{title[:1].upper()}{title[1:]}.\" " if title else ""
    host = (urlsplit(str(source.get("url") or "")).hostname or "").removeprefix("www.")
    by = _plain(str(source.get("source") or "")) or host
    date = str(source.get("date") or "")[:10]
    return f"{named}{by}{f', {date}' if date else ''} — {link}"


def _sources_list(text: str, sources: dict[str, dict[str, Any]]) -> str:
    """The Sources list under an answer: each result it cites by number, with its title and link. None when it wrote its own or cites none.

    A title and an address are a page's own words, so the list prints them defanged (``_plain``, ``_link``): a result is never listed under a ``javascript:``,
    ``file:`` or ``data:`` address, and a title cannot carry markup into the answer the user reads.
    """
    if not sources or LISTED.search(text):
        return ""
    cited = _cited(text)
    used = sorted((source for source in sources.values() if source["n"] in cited), key=lambda source: source["n"])
    # One plain bullet each, in the order of their numbers (Harlan, 2026-11-11, GRO-731: "a bullet point for each unnumbered"), cited in full rather than as a bare title and link
    # (Harlan, 2026-11-24, GRO-985: "site this in a profiessional way, not the hyperlink"): "Title." its source, its date. its link.
    lines = [f"- {_citation(source, link)}" for source in used if (link := _link(source["url"]))]
    return "Sources:\n" + "\n".join(lines) if lines else ""


def _fail(error: str) -> dict[str, Any]:
    """The failure result Hermes shows the model in place of rows."""
    return {"success": False, "error": error}


def _key() -> str:
    """The search key from this profile's secrets, or "" when none is set."""
    return str(get_secret(KEY_ENV) or "").strip()


def _key_fits(key: str) -> bool:
    """Whether the search key can be sent in an ``Authorization`` header: plain ASCII, printable, no space. urllib's error for a header it cannot send quotes the
    header whole, key and all, and would reach the model, so a key that fails this is never sent; ``BAD_KEY`` says why without it."""
    return key.isascii() and key.isprintable() and " " not in key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. urllib re-sends a redirected request to its new address with the headers it had, the search key's ``Authorization`` among them, so a
    redirect to another host would hand that host the key. Hermes's own signed-in requests follow none either; a redirect from Grounder is an error here."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Every request the plugin makes itself goes through here, so none follows a redirect."""
    return _OPENER.open(request, timeout=timeout)


def _post(payload: dict[str, Any], key: str = "", extra: Optional[dict[str, str]] = None, headers_out: Optional[dict[str, str]] = None) -> tuple[int, bytes]:
    """Send one search, with the user's own search key or, with none, as a free search that carries no credential; return the HTTP status and the body.

    ``extra`` adds request headers (a wallet's ``PAYMENT-SIGNATURE``), and ``headers_out`` gets the response's headers, their names in lower case (the offer of a 402, a paid search's receipt).
    """
    if key and not _key_fits(key):
        raise ValueError(BAD_KEY)  # never urllib's own error for the header, which quotes the key
    request = urllib.request.Request(
        SEARCH_URL, json.dumps(payload).encode(),
        {**({"Authorization": f"Bearer {key}"} if key else {}), "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": USER_AGENT, **(extra or {})})
    try:
        with _open(request, TIMEOUT_S) as response:
            if headers_out is not None:
                headers_out.update({name.lower(): value for name, value in response.headers.items()})
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        if headers_out is not None:
            headers_out.update({name.lower(): value for name, value in error.headers.items()})
        return error.code, error.read()


def _post_signed_in(payload: dict[str, Any]) -> tuple[int, bytes]:
    """Send one search through Hermes, which attaches the signed-in token; return the HTTP status and the body."""
    response = credentialed_provider_request(
        OAUTH_ID, "POST", SEARCH_URL, json=payload,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    return response.status, response.body


def sharing_usage() -> bool:
    """Whether ``web.grounder.share_usage_context`` is on (``hermes config set web.grounder.share_usage_context true`` turns it on): only then does a search tell Grounder which
    model asked for it and carry a random token for its session and one for its prompt, and only then is the ``post_api_request`` hook that learns the model registered (at load, so
    turning it on takes a restart of Hermes for the hook; the fields themselves follow it at the next search, and turning it off stops them then). Unset, unreadable or not a clear yes or no, it is ``SHARE_USAGE_DEFAULT``."""
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web")
    except Exception as error:
        logger.warning("Could not read the Grounder settings: %s", error)
        return SHARE_USAGE_DEFAULT
    section = web.get(SETTINGS_KEY) if isinstance(web, dict) else None
    value = section.get(SHARE_USAGE_KEY) if isinstance(section, dict) else None
    if value is True or value is False:
        return value
    word = value.strip().lower() if isinstance(value, str) else ""
    return True if word in ("true", "yes", "on", "1") else False if word in ("false", "no", "off", "0") else SHARE_USAGE_DEFAULT


SETTINGS_TIMEOUT_S = 8  # one read or write of the saved setting


def _settings_error(status: int, body: Any, via: str) -> str:
    """Why a read or write of the saved setting failed, in words for the user: Grounder's own message for a 400 (it names what it refused), else ``_error_message``'s."""
    detail = _error_parts(body)[1]
    return detail if status == 400 and detail else _error_message(status, body, via)


def _settings_call(method: str, body: Optional[dict[str, Any]] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``GET`` or ``PUT`` (with a JSON ``body``) of the account's saved search setting: ``(Grounder's answer, None)``, or ``(None, the reason)``. It goes with the search key when one is set
    and otherwise through Hermes's sign-in, follows no redirect, and raises ``ProviderNotSignedIn`` when the user has neither. It is no search and spends none."""
    key = _key()
    if key and not _key_fits(key):
        return None, BAD_KEY
    url = SETTINGS_URL
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        if key:
            request = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, method=method,
                                             headers={**headers, "Authorization": f"Bearer {key}", **({"Content-Type": "application/json"} if body is not None else {})})
            try:
                with _open(request, SETTINGS_TIMEOUT_S) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
        else:
            response = credentialed_provider_request(OAUTH_ID, method, url, headers=headers, timeout=SETTINGS_TIMEOUT_S, **({"json": body} if body is not None else {}))
            status, raw = response.status, response.body
    except ProviderNotSignedIn:
        raise
    except AuthError as error:
        return None, f"Grounder sign-in failed: {error}"
    except PermissionError as error:
        return None, f"Hermes refused the Grounder sign-in: {error}"
    except (httpx.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        return None, f"Could not reach Grounder: {error}"
    try:
        answer = json.loads(raw)
    except ValueError:
        answer = None
    if status != 200:
        return None, _settings_error(status, answer, "key" if key else "sign-in")
    if not isinstance(answer, dict) or not isinstance(answer.get("search"), dict):
        return None, "Grounder's answer was not what was expected"
    return answer, None


def _sign_ins_call(body: dict[str, Any], timeout: float) -> Optional[dict[str, Any]]:
    """``PUT`` Grounder direct's saved sign-in names (``direct_dev.sync_body``) and return Grounder's answer, or None when there is no credential or it failed.
    Never raises: a sync is best effort. It goes with the search key when one is set, else through Hermes's sign-in, and follows no redirect."""
    key = _key()
    if key and not _key_fits(key):
        return None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "Content-Type": "application/json"}
    try:
        if key:
            request = urllib.request.Request(SIGN_INS_URL, json.dumps(body).encode(), method="PUT", headers={**headers, "Authorization": f"Bearer {key}"})
            try:
                with _open(request, timeout) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
        else:
            response = credentialed_provider_request(OAUTH_ID, "PUT", SIGN_INS_URL, headers=headers, timeout=timeout, json=body)
            status, raw = response.status, response.body
        answer = json.loads(raw)
    except Exception as error:  # no sign-in, a refusal, no network, a body that is not JSON: the list stays as it is
        logger.info("Grounder direct's sign-in list was not synced: %s", error)
        return None
    return answer if status == 200 and isinstance(answer, dict) else None


direct_dev.account_sender = _sign_ins_call


def _can_sync_sign_ins() -> bool:
    """Whether there is a credential to report the saved sign-ins with: a search key, or a Hermes sign-in (which only a request can show, so it is asked with the
    settings read, which spends no search). Asked before the keychain is listed."""
    if _key():
        return True
    try:
        _settings_call("GET")
    except ProviderNotSignedIn:
        return False
    return True


direct_dev.credential_probe = _can_sync_sign_ins


SITE_NAME = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def _site_arg(value: Any) -> str:
    """``value`` as a site host (lowercase, at most 253 characters), or "" when it is not one."""
    word = str(value or "").strip().lower()
    return word if len(word) <= 253 and SITE_NAME.match(word) else ""


MAX_READ_TRIES = 3  # pages of one site tried when a direct read fails
DIRECT_CACHE_S = 30.0
_direct_cache: dict[str, Any] = {"at": 0.0, "value": None}


def account_direct() -> dict[str, Any] | None:
    """The ``direct`` block (``use``, ``format``, ``user_agent``) of the account's saved setting, kept 30 seconds, or None when there is no credential, the read failed or the
    answer has none. The account is the one source when there is a credential (the Routing page and ``/search`` write it); the local ``web.grounder.direct`` is only the fallback."""
    now = time.monotonic()
    if now - float(_direct_cache["at"]) < DIRECT_CACHE_S:
        return _direct_cache["value"]
    value: dict[str, Any] | None = None
    try:
        answer, _why = _settings_call("GET")
        block = answer.get("direct") if answer else None
        choice = answer.get("search") if answer else None
        mode = choice.get("mode") if isinstance(choice, dict) else None
        value = dict(block) if isinstance(block, dict) else None
        if mode in MODES:
            value = {**(value or {}), "use": direct_dev.ONLY if mode == DIRECT_MODE else direct_dev.OFF}
    except ProviderNotSignedIn:
        value = None
    _direct_cache.update(at=now, value=value)
    return value


def save_direct(change: dict[str, Any]) -> bool | None:
    """Write the search choice as search.mode and page settings as direct.format or direct.user_agent."""
    body: dict[str, Any] = {}
    if "use" in change:
        mode = DIRECT_MODE if change["use"] == direct_dev.ONLY else PROVIDER_MODE
        body["search"] = {"mode": mode}
    direct = {key: change[key] for key in ("format", "user_agent") if key in change}
    if direct:
        body["direct"] = direct
    _direct_cache.update(at=0.0, value=None)
    direct_dev.account_save_error = ""
    try:
        answer, why = _settings_call("PUT", body)
    except ProviderNotSignedIn:
        return None
    if answer is None:
        direct_dev.account_save_error = str(why or "")
        logger.info("Grounder direct's setting was not saved on the account: %s", why)
    return answer is not None


direct_dev.account_direct_reader = account_direct
direct_dev.account_direct_writer = save_direct


def direct_state() -> str:
    """How Grounder direct is set for a search: the account's saved search mode when it has one, else this Mac's setting (``direct_dev.state`` follows the account itself)."""
    return direct_dev.state()


def direct_format() -> str | None:
    """The format saved for a Grounder direct read: the account's, else this machine's, else None."""
    return direct_formats.saved()


ACT_CODES = ("daily_limit_reached", "capacity_reached", "cap_reached")  # a search refused for the user's own allowance, which only the user can change
GROUNDER_SITE = "https://grounder.net/account"
NOTICE_KEY = "grounder_notice"  # on a result that is Grounder's message to the user in place of rows ("act": what the user must do first; "refused"), or a Grounder direct read ("direct"), whose rows are pages read from their own sites and are no search source
PAYMENT_KEY = "grounder_payment"  # on the data of a result Grounder served for a wallet's payment: what was paid, and the settlement's transaction hash (``payment.receipt``)


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


def _failed_calls(details: dict[str, Any]) -> list[str]:
    """The providers a failed search called and how each ended, as ``Wikimedia (rate limited)``: the ``calls`` of Grounder's ``error.details``, each with its
    ``error_code``, else its ``status``. A call with no provider named is left out, and a ``calls`` of another shape is none."""
    named = []
    for call in _listed(details.get("calls")):
        provider = call.get("provider") if isinstance(call, dict) else None
        if not isinstance(provider, str) or not provider.strip():
            continue
        reason = next((value for value in (call.get("error_code"), call.get("status")) if isinstance(value, str) and value.strip()), "")
        named.append(_provider_name(provider.strip()) + (f" ({reason.strip().replace('_', ' ')})" if reason else ""))
    return named


def _must_act(status: int, body: Any, via: str) -> bool:
    """Whether a failed search is an answer the user must act on (a limit, a rejected key or sign-in), not a transient failure."""
    error, _detail, details = _error_parts(body)
    return status in (401, 402) or (status in (429, 503) and (error.get("code") in ACT_CODES or (via == "free" and bool(details.get("signup_url")))))


def _error_message(status: int, body: Any, via: str, declined: str = "") -> str:
    """A one-line message for a failed search, with Grounder's own reason when it gave one. ``via`` is how the search was made: ``key``, ``sign-in`` or ``free``.
    ``declined`` is why a wallet set in ``GROUNDER_X402_PRIVATE_KEY`` did not pay a 402 (``payment``): it takes the place of the line that offers a wallet."""
    error, detail, details = _error_parts(body)
    if status == 402:
        # Grounder wants payment for the search: with no account, its free searches are used up. A bare "HTTP 402" tells the user nothing, so the way past it is the message: an
        # account (the links are the API's own, and no limit is in any sentence here), or a wallet that pays for each search.
        if via != "free":
            return detail or "Grounder needs payment for this search."
        credit = f", then add credit at {details['billing_url']}" if details.get("billing_url") else ""
        account = (f"Sign up at {details['signup_url']}{credit}. Then sign in with `{SIGN_IN}`, or set a search key in `{KEY_ENV}`." if details.get("signup_url")
                   else f"Sign in with `{SIGN_IN}`, or set a search key from {KEYS_URL} in `{KEY_ENV}`.")
        wallet = declined or (f"A wallet can pay for each search instead: the user sets `{payment.KEY_ENV}` themselves, never in the chat (the plugin README says how).")
        return f"Grounder needs payment for this search. {account} {wallet}" if not declined else f"Grounder needs payment for this search. {declined} {account}"
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
    failed = _failed_calls(details) if status == 503 and error.get("code") == "no_provider_available" else []
    if failed:
        return f"Grounder's search failed: {'; '.join(failed)}. Search again in a moment."
    return f"Grounder returned HTTP {status}" + (f": {detail}" if detail else "")


def _act(message: str, url: str = "") -> _Act:
    failure = _Act(message)
    failure.url = url
    return failure


def _notice(failure: str, kind: str = "act") -> dict[str, Any]:
    """A ``web_search`` result that carries Grounder's message as its one row, in place of a failure Hermes would serve from its keyless ring.

    Hermes (0.23) serves any failed search of a non-ring backend from a free ring of other vendors' results, which hid a free-search limit's sign-up link
    and a rejected key behind unrelated rows; a successful result is never served that way. ``kind`` is ``act`` (the user must do something first; Hermes keeps every
    successful result for twenty minutes by query and publishes no way to drop one, so the message tells the model to put the question in other words once the user has acted)
    or ``refused`` (this call is not made at all).
    """
    text = (f"Grounder did not run this search. {failure} Tell the user this; the same search fails again until they have. "
            "Hermes keeps this message for twenty minutes, so once they have, search again with the question in other words." if kind == "act" else str(failure))
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
    """A random token for this session and one for this user prompt, so Grounder can tell one prompt from the next, and the model that asked for the search: only when the user
    has ``web.grounder.share_usage_context`` on (``sharing_usage``), else nothing. Neither token carries a Hermes id or a message."""
    session = _session()
    if not session or not sharing_usage():
        return {}
    conversation, turn = _TURNS.get(session, (session, ""))
    model = _MODELS.get(session)
    return {"session_ref": _token(conversation), **({"prompt_ref": _token(conversation, turn)} if turn else {}), **({"agent": {"model": model}} if model else {})}




class _Lost(Exception):
    """A payment was signed and sent, and the answer never came: Grounder may have taken it. The message says so, and nothing is paid again."""


def _pay(payload: dict[str, Any], raw: bytes, offered: dict[str, str], sent_out: Optional[list[dict[str, Any]]]) -> tuple[int, bytes, Optional[dict[str, Any]], str]:
    """The wallet's answer to a free search's 402, at most once: ``(status, body, receipt, declined)``.

    The offer is read, checked against what ``payment.choose`` pays (USDC on Base, no more than the user's cap) and signed; then the same search is sent once more with the whole request
    (the options a free search drops stay: a paid search may carry them) and the signature in ``PAYMENT-SIGNATURE``. ``receipt`` is what the 200's ``PAYMENT-RESPONSE`` says was paid.
    Without a payment ``declined`` is why, in words for the user, and ``(402, raw, None, why)`` goes back as the answer. There is no loop and no second signature: a second 402 is
    reported, never paid. Raises ``_Lost`` when the signed request got no answer, since Grounder may have settled it.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    try:
        required = payment.offer(offered, body)
        entry = payment.choose(required, payment.cap())
        header = payment.sign(entry, required, payment.wallet_key())
    except payment.Declined as why:
        return 402, raw, None, str(why)
    except Exception as error:  # a bug here must cost the user a message, never a search or a payment
        logger.warning("Grounder payment could not be prepared: %s", type(error).__name__)
        return 402, raw, None, "The payment could not be prepared, so nothing was paid."
    if sent_out is not None:
        sent_out.append(payload)
    seen: dict[str, str] = {}
    try:
        status, answer = _post(payload, extra={payment.SIGNATURE_HEADER: header}, headers_out=seen)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        logger.warning("Grounder paid request error: %s", error)
        raise _Lost("A payment was signed and sent, and Grounder did not answer. It may have been taken: check the wallet before searching again. "
                    f"Nothing more will be paid for this search. ({error})") from None
    if status == 200:
        try:
            parsed = json.loads(answer)
            readable = isinstance(parsed, dict)
        except ValueError:
            readable = False
        if not readable:  # Grounder settled and the results did not arrive whole: the payment may be gone and there is nothing to show for it
            raise _Lost("A payment was signed and sent, and Grounder's answer could not be read. It may have been taken: check the wallet before searching again. "
                        "Nothing more will be paid for this search.")
        usage = parsed.get("usage")
        if parsed.get("no_topic") or (isinstance(usage, dict) and usage.get("cost_usd") == 0):
            return status, answer, None, ""
        return status, answer, payment.receipt(seen, entry), ""
    if status == 402:
        why = payment.reason(seen)
        return status, answer, None, (f"Grounder did not accept the wallet's payment{f' ({why})' if why else ''}. Check that the wallet holds enough USDC on Base. "
                                       "It was not sent again.")
    return status, answer, None, ""


def _send(payload: dict[str, Any], sent_out: Optional[list[dict[str, Any]]] = None,
          paid_out: Optional[list[dict[str, Any]]] = None,
          via_out: Optional[list[str]] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Send one search, retrying once with the wallet only when Grounder requests payment. ``via_out`` gets how it was sent: ``key``, ``sign-in`` or ``free``."""
    key = _key()
    if key and not _key_fits(key):
        return None, _act(BAD_KEY)
    context = _context()
    if context:
        payload = {**payload, "context": {**payload.get("context", {}), **context}}
    via = "key" if key else "sign-in"
    declined = ""
    try:
        if key:
            if sent_out is not None:
                sent_out.append(payload)
            status, raw = _post(payload, key)
        else:
            try:
                if sent_out is not None:
                    sent_out.append(payload)
                status, raw = _post_signed_in(payload)
            except ProviderNotSignedIn:
                via = "free"
                if sent_out is not None:
                    sent_out.append(payload)
                offered: dict[str, str] = {}
                status, raw = _post(payload, headers_out=offered)
                if status == 402 and payment.configured():
                    status, raw, receipt, declined = _pay(payload, raw, offered, sent_out)
                    if receipt is not None:
                        if paid_out is not None:
                            paid_out.append(receipt)
                        logger.info("Grounder search paid: %s, transaction %s", receipt["amount"], receipt.get("transaction", "not named"))
    except _Lost as lost:
        return None, _act(str(lost))
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
        message = _error_message(status, body, via, declined)
        if not _must_act(status, body, via):
            return None, message
        link = str(_error_parts(body)[2].get("signup_url") or "") if via == "free" else ""
        return None, _act(message, link if link.startswith("https://") else "")
    if via_out is not None:
        via_out.append(via)
    return body, None


def _first_use_line(via: str, paid: bool) -> str:
    """What the first answered search of an install says about who pays for searches, once: free with no account, on the account (a search key or a sign-in), or
    paid by the wallet. ``web.grounder.first_use_shown`` in Hermes's config keeps it to once (the plugin writes no file of its own, GRO-595); an empty string after
    that, and where the config cannot be written, which would repeat it on every search."""
    from hermes_cli.config import is_managed, load_config, save_config
    web = load_config().get("web")
    mine = web.get("grounder") if isinstance(web, dict) else None
    if is_managed() or (isinstance(mine, dict) and mine.get("first_use_shown")):
        return ""
    state = "wallet" if paid else {"key": "key", "sign-in": "account"}.get(via, "free")
    save_config({"web": {"grounder": {"first_use_shown": True}}}, merge_existing=True)
    return {
        "free": f"Searches are free with no account. To search on an account, sign in with `{SIGN_IN}`, or set a search key in `{KEY_ENV}`.",
        "account": "Searches run on your Grounder account, signed in through Hermes.",
        "key": f"Searches run on your Grounder account through the search key in `{KEY_ENV}`.",
        "wallet": f"This search was paid for by your wallet (`{payment.KEY_ENV}`). To search on an account instead, sign in with `{SIGN_IN}`.",
    }[state]


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


# An address written without its scheme (``reddit.com/r/MechanicalKeyboards``): a host under a common top-level domain, with or without a path.
_BARE_ADDRESS = re.compile(r"(?<![\w@/.:-])(?:www\.)?(?:[a-z0-9-]+\.)+(?:com|org|net|io|co|edu|gov|dev|ai|app|news|tv|me|us|uk|ca|de|fr|jp|au)(?:/[^\s<>\"'`]*)?(?![\w.-])", re.I)


def _dev_addresses(text: str) -> list[str]:
    """The addresses written in ``text`` for Grounder direct, in order, once each, at most ``MAX_PAGES``. Full ``http(s)://`` addresses when there are any;
    else ones written without a scheme, read over https."""
    found = [_trim_address(a) for a in ADDRESS.findall(text or "")]
    if not any(found):
        found = [f"https://{_trim_address(a)}" for a in _BARE_ADDRESS.findall(text or "")]
    return [a for a in dict.fromkeys(found) if a]


SECRET_REFUSED = ("Blocked: an address in this call carries what looks like an API key or token, and secrets must not be sent in addresses. "
                  "Nothing was sent to Grounder; leave that address out.")
PRIVATE_REFUSED = ("Blocked: an address in this call is not a public web address (a private, loopback or link-local one, or one that does not resolve), "
                   "so Grounder cannot read it. Nothing was sent to Grounder; leave that address out.")
# The shapes of a key: Grounder's own, and the prefixes Hermes itself redacts (its ``agent.redact._PREFIX_PATTERNS``, vendored 2026-11-13 so the plugin imports nothing private:
# the catalog's rule 9 lists ``agent.redact._PREFIX_RE`` as a core internal). Hermes refuses a ``web_extract`` of an address that carries one of these; so does the plugin, for the same
# address as a ``web_search`` query or a ``grounder_search`` ``urls``. A family Hermes adds later is refused by Hermes's own ``web_extract`` and not here until this list follows it.
_OWN_KEY = re.compile(r"(?<![A-Za-z0-9])gr-[A-Za-z0-9]{24,}")
KEY_PATTERNS = (
    "sk-[A-Za-z0-9_-](?:\\.?[A-Za-z0-9_-]){9,}",
    "ctx7sk-[A-Za-z0-9_-]{10,}",
    "ghp_[A-Za-z0-9]{10,}",
    "github_pat_[A-Za-z0-9_]{10,}",
    "gho_[A-Za-z0-9]{10,}",
    "ghu_[A-Za-z0-9]{10,}",
    "ghs_[A-Za-z0-9]{10,}",
    "ghr_[A-Za-z0-9]{10,}",
    "xapp-\\d+-[A-Za-z0-9-]{10,}",
    "xox[baprs]-[A-Za-z0-9-]{10,}",
    "AIza[A-Za-z0-9_-]{30,}",
    "pplx-[A-Za-z0-9]{10,}",
    "fal_[A-Za-z0-9_-]{10,}",
    "fc-[A-Za-z0-9]{10,}",
    "bb_live_[A-Za-z0-9_-]{10,}",
    "gAAAA[A-Za-z0-9_=-]{20,}",
    "AKIA[A-Z0-9]{16}",
    "sk_live_[A-Za-z0-9]{10,}",
    "sk_test_[A-Za-z0-9]{10,}",
    "rk_live_[A-Za-z0-9]{10,}",
    "SG\\.[A-Za-z0-9_-]{10,}",
    "hf_[A-Za-z0-9]{10,}",
    "r8_[A-Za-z0-9]{10,}",
    "npm_[A-Za-z0-9]{10,}",
    "pypi-[A-Za-z0-9_-]{10,}",
    "dop_v1_[A-Za-z0-9]{10,}",
    "doo_v1_[A-Za-z0-9]{10,}",
    "am_(?:org_)?[A-Za-z0-9]{20,}",
    "sk_[A-Za-z0-9_]{10,}",
    "tvly-[A-Za-z0-9]{10,}",
    "exa_[A-Za-z0-9]{10,}",
    "gsk_[A-Za-z0-9]{10,}",
    "syt_[A-Za-z0-9]{10,}",
    "retaindb_[A-Za-z0-9]{10,}",
    "hsk-[A-Za-z0-9]{10,}",
    "mem0_[A-Za-z0-9]{10,}",
    "brv_[A-Za-z0-9]{10,}",
    "xai-[A-Za-z0-9]{30,}",
    "ntn_[A-Za-z0-9]{10,}",
    "fw-[A-Za-z0-9]{30,}",
    "fw_[A-Za-z0-9]{30,}",
    "fpk_[A-Za-z0-9]{30,}",
    "glpat-[A-Za-z0-9_\\-]{10,}",
    "gloas-[A-Za-z0-9_\\-]{10,}",
    "gldt-[A-Za-z0-9_\\-]{10,}",
    "glrt-[A-Za-z0-9_.\\-]{10,}",
    "glrtr-[A-Za-z0-9_.\\-]{10,}",
    "glcbt-[A-Za-z0-9_\\-]{10,}",
    "glptt-[A-Za-z0-9_\\-]{10,}",
    "glft-[A-Za-z0-9_\\-]{10,}",
    "glimt-[A-Za-z0-9_\\-]{10,}",
    "glagent-[A-Za-z0-9_\\-]{10,}",
    "glsoat-[A-Za-z0-9_\\-]{10,}",
    "glffct-[A-Za-z0-9_\\-]{10,}",
    "glwt-[A-Za-z0-9_\\-]{10,}",
    "GR1348941[A-Za-z0-9_\\-]{10,}",
    "pk-lf-[A-Za-z0-9\\-]{8,}",
)
_KEY_SHAPES = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(KEY_PATTERNS) + r")(?![A-Za-z0-9_-])")


_IGNORED_IN_HOSTS = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))  # characters a browser drops from a host
_DOTS_IN_HOSTS = {ord(dot): "." for dot in "\u3002\uff0e\uff61"}


def _private_host(host: str) -> bool:
    """Whether a host name is a private, loopback, link-local or otherwise non-public address, or a name only a local network has.

    A host is judged as written AND as a browser reads it (percent-escapes decoded, compatibility characters such as fullwidth digits mapped to plain ones, ignorable
    characters dropped), so ``%31%32%37.0.0.1`` and ``１２７.０.０.１`` are the 127.0.0.1 they are to WebKit; one with a ``%`` in it is refused outright.
    """
    if "%" in host:
        return True
    folded = unicodedata.normalize("NFKC", host).lower().translate(_DOTS_IN_HOSTS).translate(_IGNORED_IN_HOSTS)
    return _private_name(host) or (folded != host.lower() and _private_name(folded))


def _private_name(host: str) -> bool:
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

    Hermes refuses a ``web_extract`` of an address that carries a key (the prefixes in ``KEY_PATTERNS``, checked on the address and its percent-decoded form) and one
    that is not a public address (``tools.url_safety.is_safe_url``, a published plugin API). The same address as a ``web_search`` query or a ``grounder_search`` ``urls`` is read by
    Grounder's reader just the same, so each goes through the same checks first. A host with a password in it, a literal private address, ``localhost`` and a local-only name are
    refused here whether or not this Hermes has ``is_safe_url``. The reason never repeats the address.
    """
    forms = [address, unquote(address)]
    own, wallet = _key(), payment.secret_forms()  # the search key, and the wallet's private key as it could be written
    if any(_KEY_SHAPES.search(form) or _OWN_KEY.search(form) or (own and own in form) or any(secret in form.lower() for secret in wallet) for form in forms):
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
    for name in ("top_k", "include_text"):
        if args.get(name) not in (None, "", [], {}):
            payload[name] = args[name]
    filters: dict[str, Any] = {}
    if isinstance(args.get("days"), int) and args["days"] > 0:
        filters["recency"] = {"mode": "rolling", "days": args["days"]}
    if filters:
        payload["filters"] = filters
    stated = {name: str(args[name]).strip()[:limit] for name, limit in (("user_location", 200), ("user_time_zone", 64)) if str(args.get(name) or "").strip()}
    if stated:
        payload["context"] = stated
    return payload


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
        # The pages Grounder fetched straight from their sites (Grounder direct) or extracted with its reader, by session and turn, for the line under the answer.
        self._fetched: dict[tuple[str, str], list[dict[str, Any]]] = {}
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
        """The user's selected search, or a free read when the query names pages by address."""
        return self._search({"query": query, "top_k": max(1, min(int(limit), MAX_RESULTS))})

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Read named pages free, on the Mac when selected or through Grounder's hosted reader."""
        asked = [str(url) for url in urls]
        pages = list(dict.fromkeys(asked))
        if len(pages) > MAX_PAGES:
            return _notice_pages(asked, f"Grounder direct reads at most {MAX_PAGES} pages a call.")
        if direct_state() == direct_dev.ONLY:
            return self._dev_extract(asked)
        refused = {page: reason for page in pages if (reason := _refusal(page))}
        pages = [page for page in pages if page not in refused]
        if not pages:
            return [_page_error(url, refused[url]) for url in asked]
        agent = direct_dev.user_agent()
        if not agent:
            return _notice_pages(asked, "Grounder direct needs your Safari user agent to read these pages.")
        question = self._question(pages)
        fmt = direct_formats.chosen(None, _ASKS.get(_session(), ""), direct_format())
        body, error = _send({"query": question, "urls": pages, "mode": DIRECT_MODE, "user_agent": agent, "format": fmt})
        if body is None:
            if isinstance(error, _Act):
                return _notice_pages(asked, error)
            return [_page_error(url, refused.get(url) or error or "Grounder returned no answer") for url in asked]
        found = _pages_found(pages, _results(body))
        logger.info("Grounder extract: %d of %d pages read", len(found), len(pages))
        chat = _chat(_session())
        with self._lock:
            shown = {page: self._shown[(chat, _page_key(page))] for page in pages if page not in found and (chat, _page_key(page)) in self._shown}
            for page in [*found, *shown]:
                self._read[(chat, _page_key(page))] = True
            _bounded(self._read, REMEMBERED_PAGES)
        why = _unread(body)
        return [
            _page_shown(url, shown[url]) if url in shown
            else _page_error(url, refused[url]) if url in refused
            else _page_row(url, found.get(url), why.get(_page_key(url), ""))
            for url in asked
        ]

    def _dev_search(self, query: str, fmt: Optional[str] = None, question: str = "", signed_out: bool = False, pages: Optional[list[str]] = None) -> dict[str, Any]:
        """Grounder direct's ``web_search``: no search at all. It reads the addresses in the query, else those in the user's latest message, each straight from its
        site (``direct_dev.read``), one row a page with the page's text. With no address it reads nothing and says so. Every outcome is a successful result with a notice
        mark, so Hermes serves none of it from its keyless ring and the hooks claim none of it for Grounder."""
        asks = _ASKS.get(_session(), "")
        addresses = pages if pages is not None else (_dev_addresses(query) or _dev_addresses(asks))
        if not addresses:
            return _notice(direct_dev.NO_ADDRESS, "refused")
        fmt = direct_formats.chosen(fmt, asks, direct_format())
        signed_out = signed_out or direct_formats.signed_out_words(asks) or direct_formats.signed_out_words(query)
        question = " ".join(ADDRESS.sub(" ", question or query).split()) or asks
        rows = []
        for url in addresses:
            why = _refusal(url)
            page = {"url": url, "problem": why} if why else direct_dev.read(url, signed_out)
            text = (direct_dev.not_read(url, page["problem"]) if page.get("problem")
                    else direct_formats.shape(direct_dev.header(page), str(page.get("text") or ""), fmt, question, str(page.get("url") or url)))
            rows.append({"title": str(page.get("title") or url), "url": str(page.get("url") or url), "description": text, "position": len(rows) + 1})
        return {"success": True, "data": {"web": rows, NOTICE_KEY: "direct"}}

    def _read_candidates(self, results: list[dict[str, Any]], fmt: str, question: str, signed_out: bool, site: str = "", credits: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Read the chosen pages, trying a later address only for the same topic and site. `credits` is each topic's credit line (GRO-972), which a page read here carries as a result does."""
        groups: dict[tuple[str, str], list[str]] = {}
        first_site: dict[str, str] = {}
        for result in results:
            url = str(result.get("url") or "")
            if not url or _refusal(url):
                continue
            host = (urlsplit(url).hostname or "").lower()
            if site and not (host == site or host.endswith("." + site)):
                continue
            topic = str(result.get("topic") or "")
            source = site or direct_dev.site_of(url)[0] or host
            if fmt != direct_formats.HIGHLIGHTS and first_site.setdefault(topic, source) != source:
                continue
            groups.setdefault((topic, source), []).append(url)
        rows = []
        for (topic, _), urls in groups.items():
            for url in list(dict.fromkeys(urls))[:MAX_READ_TRIES]:
                page = direct_dev.read(url, signed_out)
                problem = page.get("problem")
                if problem:
                    text = direct_dev.not_read(url, problem)
                else:
                    header = direct_dev.header({**page, "found_by_search": True})
                    text = direct_formats.shape(header, str(page.get("text") or ""), fmt, question, str(page.get("url") or url))
                if not problem or page.get("reason") in ("site_check", "busy") or "site check" in str(problem).lower():
                    break
            rows.append({"title": str(page.get("title") or url), "url": str(page.get("url") or url),
                         "description": text, **({"topic": topic} if topic else {}), **({"licence": (credits or {})[topic]} if (credits or {}).get(topic) else {})})
        return rows

    def _dev_extract(self, urls: list[str]) -> list[dict[str, Any]]:
        """Grounder direct's ``web_extract``: each page read straight from its site, in order. A page not read says why as its text, with no ``error`` and filed under
        Grounder's address, so Hermes's own reader fetches nothing in its place and caches nothing as the page's text: there is no fallback."""
        out: list[dict[str, Any]] = []
        done: dict[str, dict[str, Any]] = {}
        asks = _ASKS.get(_session(), "")
        fmt = direct_formats.chosen(None, asks, direct_format())
        signed_out = direct_formats.signed_out_words(asks)
        question = self._question([str(u) for u in urls]) if not asks else asks
        for url in (str(u) for u in urls):
            if url not in done:
                why = _refusal(url)
                page = {"url": url, "problem": why} if why else direct_dev.read(url, signed_out)
                if page.get("problem"):
                    text = direct_dev.not_read(url, page["problem"])
                    done[url] = {"url": GROUNDER_SITE, "title": direct_dev.NAME, "content": text, "raw_content": text}
                else:
                    text = direct_formats.shape(direct_dev.header(page), str(page.get("text") or ""), fmt, question, str(page.get("url") or url))
                    # Filed under the page's own address with a #grounder-direct mark: not the address asked for, so Hermes does not cache it, and a
                    # changed format or a fresh read is never answered with an earlier one.
                    done[url] = {"url": f"{str(page.get('url') or url).split('#')[0]}#grounder-direct", "title": str(page.get("title") or ""), "content": text, "raw_content": text}
            out.append(done[url])
        return out

    def _read_part(self, query: str, pages: list[str], fmt: str) -> dict[str, Any]:
        """A ``web_search`` whose query holds pages' addresses: read those pages for the query's other words, in ``web_search``'s shape (GRO-691).

        Hermes's ``web_extract`` takes only addresses, and answers a repeated read from its own cache, so a model that needed another part of a page
        fetched it with its own tools. Hermes keys its search cache by the query, so a new question reaches Grounder. A query of addresses alone is
        ranked as an extract is, against the search that found the pages. Each page comes back whole, as Grounder returned it: no ``text_max_chars`` is sent.
        """
        chat = _chat(_session())
        question = " ".join(ADDRESS.sub(" ", query).split())[:MAX_QUERY_CHARS] or self._question(pages)
        agent = direct_dev.user_agent()
        if not agent:
            return _notice("Grounder direct needs your Safari user agent to read these pages.", "refused")
        body, error = _send({"query": question, "urls": pages, "mode": DIRECT_MODE, "format": fmt, "user_agent": agent})
        if body is None:
            return _notice(error) if isinstance(error, _Act) else _notice(error or "Grounder returned no answer", "refused")
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
                message = UNREAD.get(reasons.pop(), NOT_READ)
                return _notice(message if len(pages) == 1 else message.replace("this page", "these pages"), "refused")
            return _notice("Grounder could not read these pages, or their sites do not allow it.", "refused")
        rows = []
        for page in pages:
            if page in found or page in shown:
                entry = _page_row(page, found[page]) if page in found else _page_shown(page, shown[page])
                rows.append({"title": entry["title"], "url": page, "description": entry["content"], "position": len(rows) + 1})
        self._remember(query[:MAX_QUERY_CHARS], rows, _summary(body, len(rows), {'mode': DIRECT_MODE}), found_by=False)
        return {"success": True, "data": {"web": rows}}

    def after_tool(self, tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", turn_id: str = "", conversation_id: str = "",
                   **_: Any) -> Optional[str]:
        """Hermes's ``transform_tool_result``: note a search for the line under the answer, number each result Grounder served across the turn, say on each
        result that Grounder served it and how (``SERVED_KEY``), and add the citation request to the turn's first one.

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
        self._note_direct(tool_name, data, (session_id, turn_id), conversation_id or _chat(session_id))
        if isinstance(data.get("data"), dict) and data["data"].get(NOTICE_KEY):
            # Grounder's message to the user, not a search: nothing in it is a source. Hermes keeps it for twenty minutes by query, and the plugin cannot drop it (Hermes
            # publishes no way to), so the notice itself says to search again in other words once the user has acted (``_notice``).
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
                if served:
                    data[SERVED_KEY] = "Grounder" + (f"; {len(served)} of these {len(entries)} pages read" if len(served) < len(entries) else "")
            else:
                inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                # Another backend's rows under a query Grounder answered before (Hermes's rescue of a search that failed this time) are not Grounder's: Hermes says
                # so in the result, and the summary kept from the earlier search must not claim them.
                rescued = bool(inner.get("rescued_from")) or (isinstance(inner.get(SERVED_KEY), str) and not inner[SERVED_KEY].startswith("Grounder"))
                summary = None if rescued else self._searched.get(str((args or {}).get("query", ""))[:MAX_QUERY_CHARS])
                rows = inner.get("web")
                served = [row for row in rows if isinstance(row, dict) and row.get("url")] if summary is not None and summary["sources"] and isinstance(rows, list) else []
                if served and summary is not None:
                    # A search is timed once, when it ran: the same query answered again from Hermes's cache took no time.
                    ran, summary["fresh"] = summary["fresh"], False
                    self._turns.setdefault(turn, []).append({"mode": summary["mode"], "via": summary["via"], "ms": summary["ms"] if ran else 0, "steps": summary.get("steps", []) if ran else [], "from": summary["from"],
                                                             "pages": {_page_key(str(row["url"])) for row in served}})
                    _bounded(self._turns, REMEMBERED_TURNS)
                    inner[SERVED_KEY] = _served_line(summary, len(served))
            if not served:
                return None
            if named:
                sources = self._sources.setdefault(turn, {})
                _bounded(self._sources, REMEMBERED_TURNS)
                for entry in served:
                    key = _page_key(str(entry["url"]))
                    if key not in sources:
                        sources[key] = {"n": len(sources) + 1, "title": " ".join(str(entry.get("title") or "").split()), "url": str(entry["url"]),
                                        "source": str(entry.get("source") or ""), "date": str(entry.get("date") or "")}
                    entry["position"] = sources[key]["n"]
            notes = not named or turn not in self._noted
            if named and notes:
                self._noted[turn] = True
                _bounded(self._noted, REMEMBERED_TURNS)
        extra = {CITE_KEY: CITE_REQUEST if named else CITE_REQUEST_LISTED} if notes else {}
        return json.dumps({**data, **extra}, indent=2, ensure_ascii=False)

    def _note_direct(self, tool_name: str, data: dict[str, Any], turn: tuple[str, str], chat: str) -> None:
        """Note each page this result's call fetched straight from its site (its text starts with Grounder direct's header) or that Grounder's reader
        extracted, for the line under the answer (``_fetched_line``)."""
        if tool_name == "web_extract":
            entries = data.get("results") if isinstance(data.get("results"), list) else []
            texts = [(str(e.get("url") or ""), str(e.get("content") or "")) for e in entries if isinstance(e, dict)]
        else:
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            texts = [(str(r.get("url") or ""), str(r.get("description") or "")) for r in inner.get("web") or [] if isinstance(r, dict)]
        found = []
        for url, text in texts:
            direct = _DIRECT_HEADER.match(text)
            if direct:
                fmt = _FORMAT_LINE.search(text)
                found.append({"verb": "fetched", "host": direct.group(1), "ms": int(direct.group(2)), "format": fmt.group(1) if fmt else ""})
            elif tool_name == "web_extract" and text and url.startswith("http") and (chat, _page_key(url)) in self._read:
                found.append({"verb": "extracted", "host": urlsplit(url).hostname or url, "ms": 0, "format": ""})
        if found:
            with self._lock:
                self._fetched.setdefault(turn, []).extend(found)
                _bounded(self._fetched, REMEMBERED_TURNS)

    def after_answer(self, response_text: str = "", session_id: str = "", turn_id: str = "", **_: Any) -> Optional[str]:
        """Hermes's ``transform_llm_output``: end an answer with the Sources list of what it cites and, when it searched, Grounder's card line.
        The list names the results the answer cites that this turn's own searches and reads returned, and none from an earlier turn: a turn that searched
        nothing has no list, and its answer cites nothing by number. Appends only, so Hermes keeps streaming the answer and shows the tail after it (GRO-730)."""
        with self._lock:
            searches = self._turns.pop((session_id, turn_id), None)
            sources = self._sources.pop((session_id, turn_id), None) or {}
        if not response_text:
            return None
        with self._lock:
            fetched = self._fetched.pop((session_id, turn_id), None)
        tail = [part for part in (_sources_list(response_text, sources), _card_line(searches) if searches else "", _fetched_line(fetched) if fetched else "") if part]
        # The answer goes back byte for byte with the tail after it: Hermes's CLI prints only the part after the text it already streamed, and a reply that no longer
        # starts with that text (a trailing newline cut off) is printed again whole under a "response transformed" heading, outside the box (GRO-727).
        gap = "\n" * max(0, 2 - (len(response_text) - len(response_text.rstrip("\n"))))
        return f"{response_text}{gap}" + "\n\n".join(tail) if tail else None

    def search_tool(self, args: dict[str, Any], **_: Any) -> str:
        answer = self._search(args)
        data = answer.get("data") or {}
        if data.get(NOTICE_KEY) in ("refused", "act"):
            answer = _fail(data["web"][0]["description"])
        return json.dumps(answer, indent=2, ensure_ascii=False)

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return _fail("Grounder search needs a query.")
        mode = str(args.get("mode") or "").strip()
        if mode and mode not in MODES:
            return _fail(f"mode must be one of {', '.join(MODES)}.")
        site = _site_arg(args.get("site"))
        explicit = args.get("urls")
        pages = list(dict.fromkeys(str(url) for url in explicit)) if isinstance(explicit, list) else []
        addresses = _dev_addresses(query)
        why = next((reason for url in dict.fromkeys([*pages, *addresses]) if (reason := _refusal(url))), "")
        if why:
            return _notice(why, "refused")
        if not pages and mode != PROVIDER_MODE:
            pages = addresses
        if len(pages) > MAX_PAGES:
            return _notice(f"Grounder direct reads at most {MAX_PAGES} pages a call.", "refused")
        if mode == PROVIDER_MODE and (pages or site):
            return _notice("Use mode grounder-direct to read pages by address or search a named site.", "refused")
        if args.get("site") and not site:
            return _fail("site must be a host such as bbc.co.uk.")
        asked_format = args.get("format")
        if asked_format and not direct_formats.valid(asked_format):
            return _fail(f"format must be one of {', '.join(direct_formats.FORMATS)}.")
        state = direct_state()
        asks = _ASKS.get(_session(), "")
        local = state == direct_dev.ONLY
        reading = mode == DIRECT_MODE or (not mode and (local or bool(pages) or bool(site)))
        fmt = direct_formats.chosen(asked_format, asks or query, direct_format()) if reading else ""
        signed_out = args.get("signed_in") is False or direct_formats.signed_out_words(asks)
        if pages:
            return self._dev_search(query, fmt, signed_out=signed_out, pages=pages) if local else self._read_part(query, pages, fmt)
        payload = _options(args, query)
        payload["mode"] = DIRECT_MODE if reading else PROVIDER_MODE
        if reading:
            payload["format"] = fmt
            if site:
                payload["site"] = site
            if args.get("signed_in") is not None:
                payload["signed_in"] = args["signed_in"]
            if local:
                payload["reader"] = "mac"
            elif agent := direct_dev.user_agent():
                payload["user_agent"] = agent
            else:
                return _notice("Grounder direct needs your Safari user agent to read pages.", "refused")
        paid: list[dict[str, Any]] = []
        sent_via: list[str] = []
        body, error = _send(payload, paid_out=paid, via_out=sent_via)
        if body is None:
            return _notice(error) if isinstance(error, _Act) else _fail(error or "Grounder returned no answer")
        if body.get("no_topic"):
            return _notice("No topic fits this question, so no source was asked and nothing was charged.", "no_topic")
        topics = [topic for topic in _listed(body.get("topics")) if isinstance(topic, dict)]
        results = _results(body)
        if not reading and isinstance(payload.get("top_k"), int):
            results = results[:payload["top_k"]]
        rows = [_row(row, i + 1) for i, row in enumerate(results)]
        if local and reading:
            credits = {str(topic.get("id")): credit for topic in topics if (credit := _credit(topic.get("licence")))}
            found = self._read_candidates(_candidates(body), fmt, query, signed_out, site, credits)
            combined = [*found, *rows]
            order = {str(topic.get("id")): i for i, topic in enumerate(topics)}
            rows = sorted(combined, key=lambda row: order.get(str(row.get("topic")), len(order)))
            for i, row in enumerate(rows, 1):
                row["position"] = i
        self._remember(query, rows, _summary(body, len(rows), payload))
        logger.info("Grounder search: %d results", len(rows))
        direct = body.get("direct") if isinstance(body.get("direct"), dict) else {}
        notes = [str(topic["note"]) for topic in topics if topic.get("note")]
        if direct.get("note"):
            notes.append(str(direct["note"]))
        if reading and not rows and not notes:
            notes.append("Grounder found no page it could read. It made no other search.")
        if first := _first_use_line(sent_via[-1] if sent_via else "free", bool(paid)):
            notes.append(first)
        return {"success": True, "data": {"web": rows, **({"topics": topics} if topics else {}),
                **({"note": " ".join(notes)} if notes else {}), **({PAYMENT_KEY: paid[-1]} if paid else {})}}

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
            "tag": "Grounder searches sources it chose by hand for your question or reads their pages. "
                   f"Free searches need no account. For more, use a search key in `{KEY_ENV}`, sign in with `{SIGN_IN}`, or pay per search with a wallet.",
            "env_vars": [],
        }
