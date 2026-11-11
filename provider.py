# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder as a Hermes web search and extract backend.

Each ``web_search`` or ``web_extract`` call is one ``POST /v1/search`` to Grounder
and counts as one search. A search reaches the providers the query needs and
returns one deduplicated list of licensed results, mapped to Hermes's rows (title,
URL, description, position). An extract reads the pages it is given and returns
what in each answers the ``web_search`` that found it, at most ``EXTRACT_CHARS``
characters; a page no search led to comes back from its start. Every failure is
reported, never raised: ``{"success": False, "error": ...}`` for a search, the
page's own ``error`` for an extract.

A result Grounder served asks the model to cite what it uses by number, and an
answer ends with the Sources list of what it cites and, when it searched, the line
Grounder's card shows (``after_tool`` and ``after_answer``, Hermes's result and reply
hooks). Both work on what is already on this machine and send nothing. A
``web_search`` whose query holds a page's address reads that page for the rest of
the query, so a model can ask for another part of a page.

The plugin selects itself the first time it loads in a profile (``__init__.py``), by
setting ``web.search_backend: grounder`` and ``web.extract_backend: grounder``.
A call uses the Grounder search key in ``GROUNDER_API_KEY`` when one is set, and otherwise the sign-in Hermes keeps
for the ``oauth:`` client in ``plugin.yaml`` (``hermes auth add grounder:grounder``):
Hermes signs in, stores and refreshes that grant and attaches its token to the
request, so this plugin never holds a token.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from agent.secret_scope import get_secret
from agent.web_search_provider import WebSearchProvider
from hermes_cli.auth_constants import AuthError
from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request

logger = logging.getLogger(__name__)

ORIGIN = "https://api.grounder.net"
SEARCH_URL = f"{ORIGIN}/v1/search"
FEATURES_URL = f"{ORIGIN}/v1/features"
OAUTH_ID = "grounder"  # the id of the oauth: entry in plugin.yaml
SIGN_IN = f"hermes auth add grounder:{OAUTH_ID}"  # the plugin's key (its name), a colon, the entry's id
USER_AGENT = "grounder-hermes/0.5.12"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
KEY_ENV = "GROUNDER_API_KEY"
CLIENT_ENV = "GROUNDER_OAUTH_CLIENT_ID"  # filled in at the first load when there is no search key
CLIENT_ID = f"{ORIGIN}/oauth/clients/hermes-agent"  # public, the same for everyone
MAX_RESULTS = 20  # most results one search asks Grounder for
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
DESCRIPTION_CHARS = 800  # a row's description, and the text Grounder is asked to fit it in (text_max_chars)
EXTRACT_CHARS = 6000  # the most text of one page web_extract returns, and what Grounder is asked to keep it within
MAX_EXTRACT_CHARS = 30000  # the most a model's char_limit raises that to (GRO-725)
MAX_PAGES = 10  # Grounder's limit on the pages one call reads; Hermes asks for at most 5
REMEMBERED_PAGES = 500  # how many pages the plugin remembers the web_search of, in memory only
REMEMBERED_TURNS = 200  # how many searches and unfinished turns it remembers for the line under an answer, in memory only
# Two notes the model reads once an answer, on the first of the turn's results Grounder served (GRO-698): every search of a turn repeating them cost
# about 420 characters a search, and an answer needs them once. `after_tool` adds them, as the result's last fields, so the citation request is
# the last thing read; a host that names no turn gets them on every result.
CITE_KEY = "cite_sources"
# The model cites by number and `after_answer` writes the Sources list from what Grounder served (GRO-701): a list of titles and links was a third of an
# answer, in the model's slowest and dearest tokens, and the plugin already holds every word of it. A result's `position` is its number across the turn.
CITE_REQUEST = ("Cite every result your answer uses by its position in square brackets, like [3], in this answer and in every later one in this chat, "
                "not only the first. Positions run on across this answer's searches and page reads, so each number names one result. "
                "Write no Sources list: one with each cited result's title and link is added under the answer.")
# For a host that names no turn the plugin cannot tell which answer a result serves, so the model writes the list itself. The first sentence is word for
# word what Grounder's MCP server ends every search's notes with (CITE_REMINDER in routes/mcp.ts).
CITE_REQUEST_LISTED = ("Cite every result your answer uses, each with its link, in this answer and in every later one in this chat, not only the first. "
                       "End the answer with a Sources list: one line per result used, with its title and its link.")
# Hermes's web_extract tells the model a large page's full text is saved to a file for read_file. Grounder sends only the part that answers and saves
# nothing, so a model that wanted more guessed file names (GRO-688: `Asian_Latin_Americans.md` in two folders, then a 32-second search of the disk), and
# one that wanted another part of a page fetched it with its own tools (GRO-691). A web_search whose query holds a page's address reads that page for
# the query's other words, so the model can ask for the part it lacks.
FILES_KEY = "pages"
FILES_NOTE = ("Each text is the part of its page that answers the query. For another part of a page, web_search its address and what you need, "
              "like \"https://example.com/page early life\". Grounder saves no page to a file, so read_file finds none.")
# A page read that failed, for a page a search returned: the model gets what that search showed of it (GRO-694). In test-b a bare error sent the model
# to fetch the page itself, and Hermes wrote itself a skill to do so whenever Grounder erred (GRO-688).
SHOWN_NOTE = "Grounder could not read this page in full. This is what its search showed of it."
ADDRESS = re.compile(r"https?://[^\s<>\"'`]+", re.I)
# A heading or label that starts a Sources list the model wrote itself, so the plugin adds none.
LISTED = re.compile(r"^[ \t>]*(?:#{1,6}[ \t]*|\*\*|__)?(?:sources|references|citations)\b", re.I | re.M)
# A citation: [3], [3, 5] or [3-5].
CITED = re.compile(r"\[(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\]")
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
        "requested_provider": {"type": "string", "description": f"Provider the user explicitly asked for, by id ({LIST_PROVIDERS_TOOL} lists them)."},
        "providers": {"type": "array", "items": {"type": "string"}, "description": "Explicit provider override: ids as for requested_provider."},
        "routing_settings": {"type": "object", "description": "This search's routing settings over the account's saved ones; not saved. Pass only what differs, "
                                                              'when the user or the task calls for it, e.g. {"providers": {"exa": {"use": "always"}}}.'},
        "workspace": {"type": "string", "description": "Slug of the workspace to search under; the default one when left out. An unknown slug is refused, never replaced by the default."},
        "user_location": {"type": "string", "description": 'Where the user is, only if the user said so in this chat, e.g. "Austin, Texas". Never guess it.'},
        "user_time_zone": {"type": "string", "description": "The user's time zone, only if the user said so in this chat, as an IANA name, e.g. \"America/Chicago\". Never guess it."},
    }},
}
LIST_PROVIDERS_SCHEMA = {
    "name": LIST_PROVIDERS_TOOL,
    "description": "List the features a Grounder search can use: what each returns, the inputs it honors, and the providers (with modes) and live-data APIs that deliver it.",
    "parameters": {"type": "object", "properties": {}},
}
# Where a provider's or live source's id is not its name capitalized; the rest of Grounder's lists are.
PROVIDER_NAMES = {"you": "You.com", "xai": "xAI", "newsapi": "NewsAPI.ai", "tinyfish": "TinyFish", "aeroapi": "FlightAware",
                  "ap-elections": "AP Elections", "bestbuy": "Best Buy", "espn": "ESPN", "fed": "Federal Reserve", "mlb": "MLB",
                  "nws": "National Weather Service", "odds-api": "The Odds API", "openf1": "OpenF1", "queue-times": "Queue-Times.com",
                  "seatgeek": "SeatGeek", "statuspage": "Status pages", "truth-social": "Truth Social", "usgs": "USGS",
                  "yahoo-finance": "Yahoo Finance", "youtube": "YouTube"}
TIMEOUT_S = 45  # one search, across every provider it reaches


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


def _row(result: dict[str, Any], position: int, chars: int = 0) -> dict[str, Any]:
    """Hermes's row for one result, with its date when Grounder has one.

    A result's provenance (whose it is, the feature and provider that found it, why it may be served) stays in Grounder's records and in the API's
    response, and the line under an answer names the providers; the model reads none of it per result (GRO-698: about 100 characters a result).
    """
    date = str(result.get("published_date") or "")[:10]
    return {"title": str(result.get("title", "")), "url": str(result.get("url", "")),
            "description": _description(result, chars or DESCRIPTION_CHARS), "position": position, **({"date": date} if date else {})}


def _summary(body: dict[str, Any], sources: int) -> dict[str, Any]:
    """What the line under an answer needs of one search: who answered, how many sources it returned and how long it took."""
    routing = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    calls = [*(routing.get("calls") or []), *(routing.get("live_calls") or [])]
    answered = [str(c.get("provider")) for c in calls if isinstance(c, dict) and c.get("status") == "ok" and c.get("result_count")]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    latency = usage.get("latency_ms")
    return {"via": [_provider_name(p) for p in dict.fromkeys(answered)], "sources": sources,
            "ms": latency if isinstance(latency, int) else 0}


def _card_line(searches: list[dict[str, Any]]) -> str:
    """The line Grounder's card shows for a search, for every search of one answer: the providers that answered, the sources and the time."""
    via = list(dict.fromkeys(name for search in searches for name in search["via"]))
    parts = ["Searched with Grounder"]
    if via:
        parts.append("via " + ", ".join(via[:2]) + (f" +{len(via) - 2}" if len(via) > 2 else ""))
    parts.append(_plural(sum(search["sources"] for search in searches), "source"))
    total = sum(search["ms"] for search in searches)
    if total:
        parts.append(_duration(total))
    return " · ".join(parts)


def _cited(text: str) -> set[int]:
    """The numbers an answer cites in square brackets, a range counting each number in it."""
    numbers: set[int] = set()
    for group in CITED.findall(text):
        for part in re.split(r"\s*,\s*", group):
            bounds = [int(bound) for bound in re.split(r"\s*[–-]\s*", part) if bound.isdigit()]
            if len(bounds) == 2 and bounds[0] <= bounds[1] <= bounds[0] + 50:
                numbers.update(range(bounds[0], bounds[1] + 1))
            elif len(bounds) == 1:
                numbers.add(bounds[0])
    return numbers


def _sources_list(text: str, sources: dict[str, dict[str, Any]]) -> str:
    """The Sources list under an answer: each result it cites by number, with its title and link. None when it wrote its own or cites none."""
    if not sources or LISTED.search(text):
        return ""
    cited = _cited(text)
    used = sorted((source for source in sources.values() if source["n"] in cited), key=lambda source: source["n"])
    if not used:
        return ""
    lines = [f"- [{source['n']}] {source['title'] or source['url']} — {source['url']}" for source in used]
    return "Sources:\n" + "\n".join(lines)


def _fail(error: str) -> dict[str, Any]:
    """The failure result Hermes shows the model in place of rows."""
    return {"success": False, "error": error}


def _key() -> str:
    """The search key from this profile's secrets, or "" when none is set."""
    return str(get_secret(KEY_ENV) or "").strip()


def _post_with_key(key: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    """Send one search with the user's own search key; return the HTTP status and the body."""
    request = urllib.request.Request(
        SEARCH_URL, json.dumps(payload).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post_signed_in(payload: dict[str, Any]) -> tuple[int, bytes]:
    """Send one search through Hermes, which attaches the signed-in token; return the HTTP status and the body."""
    response = credentialed_provider_request(
        OAUTH_ID, "POST", SEARCH_URL, json=payload,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    return response.status, response.body


def _error_message(status: int, body: Any, signed_in: bool) -> str:
    """A one-line message for a failed search, with Grounder's own reason when it gave one."""
    detail: Optional[str] = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = body["error"].get("message")
    if status == 401:
        return (f"Grounder rejected the sign-in. Run `{SIGN_IN}`." if signed_in
                else f"Grounder rejected the search key in {KEY_ENV}.")
    if status == 429:
        return f"Grounder rate limit reached: {detail or 'try again later'}"
    return f"Grounder returned HTTP {status}" + (f": {detail}" if detail else "")


def _description(result: dict[str, Any], chars: int = DESCRIPTION_CHARS) -> str:
    """The result's summary, else its text, on one line.

    Grounder sends the text as the passages that best match the query, already
    within ``DESCRIPTION_CHARS``, so this chooses nothing and cuts nothing of
    it. The cap is a guard for a response that ignored ``text_max_chars`` (an
    older Grounder, or a summary longer than the budget): a row never carries
    more than the budget.
    """
    text = " ".join(str(result.get("summary") or result.get("text") or "").split())
    if len(text) > chars:
        text = text[:chars - 1].rstrip() + "…"
    return text


# The conversation and turn of each live session, as Hermes's ``pre_tool_call`` hook last named them.
_TURNS: dict[str, tuple[str, str]] = {}
_TURNS_KEPT = 64
# Makes the tokens below random to everyone: they cannot be worked back to a Hermes id, and last only as long as this process.
_SALT = secrets.token_bytes(16)


# The characters a model asked ``web_extract`` for (its ``char_limit``), by session, for the read that call makes. Hermes cuts a page at it but does not
# pass it to the backend, so without this a model that asked for more got 6,000 characters again and fetched the page itself (GRO-725).
_LIMITS: dict[str, int] = {}


def _asked_chars(value: Any) -> int:
    """A ``char_limit`` as the characters a read may return: from ``EXTRACT_CHARS`` up to ``MAX_EXTRACT_CHARS``, or ``EXTRACT_CHARS`` when it is not a number."""
    try:
        return max(EXTRACT_CHARS, min(MAX_EXTRACT_CHARS, int(value)))
    except (TypeError, ValueError):
        return EXTRACT_CHARS


def note_turn(**ids: Any) -> None:
    """``pre_tool_call`` hook: remember which conversation and turn this session is in, and a ``web_extract``'s ``char_limit``. Changes nothing about the call."""
    session = str(ids.get("session_id") or "")
    if not session:
        return
    args = ids.get("args") if isinstance(ids.get("args"), dict) else {}
    if ids.get("tool_name") == "web_extract" and args.get("char_limit") is not None:
        _LIMITS.pop(session, None)
        _LIMITS[session] = _asked_chars(args.get("char_limit"))
        while len(_LIMITS) > _TURNS_KEPT:
            _LIMITS.pop(next(iter(_LIMITS)), None)
    elif ids.get("tool_name") == "web_extract":
        _LIMITS.pop(session, None)
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


def _send(payload: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One POST to Grounder: ``(body, None)``, or ``(None, error)`` when it failed. Never raises, and never retries: a second POST is a second search."""
    key = _key()
    context = _context()
    if context:
        payload = {**payload, "context": {**payload.get("context", {}), **context}}
    try:
        status, raw = _post_with_key(key, payload) if key else _post_signed_in(payload)
    except ProviderNotSignedIn:
        return None, f"Not signed in to Grounder. Run `{SIGN_IN}`."
    except AuthError as error:
        return None, f"Grounder sign-in failed: {error}"
    except PermissionError as error:
        return None, f"Hermes refused the Grounder sign-in: {error}"
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
        return None, _error_message(status, body, signed_in=not key)
    return body, None


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


def _page_row(url: str, row: Optional[dict[str, Any]], reason: str = "", chars: int = EXTRACT_CHARS) -> dict[str, Any]:
    """Hermes's entry for one page, or its error when no result read it, saying why when the API knows."""
    if row is None:
        return _page_error(url, UNREAD.get(reason, NOT_READ))
    text = str(row.get("text") or "").strip()
    # A guard for a response that ignored ``text_max_chars``, as ``_description`` has for a row.
    if len(text) > chars:
        text = text[:chars - 1].rstrip() + "…"
    return {"url": url, "title": str(row.get("title", "")), "content": text, "raw_content": text}


def _page_error(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error}


def _page_shown(url: str, shown: tuple[str, str]) -> dict[str, Any]:
    """Hermes's entry for a page Grounder could not read but a search returned: what that search showed of it, which says so (GRO-694).
    No ``error``, so Hermes keeps it as read and its own reader does not fetch the page again."""
    title, text = shown
    content = f"{SHOWN_NOTE}\n\n{text}"
    return {"url": url, "title": title, "content": content, "raw_content": content}


def _options(args: dict[str, Any], query: str, chars: int) -> dict[str, Any]:
    """``grounder_search``'s arguments as a search request, the way Grounder's MCP search maps the same arguments."""
    payload: dict[str, Any] = {"query": query, "text_max_chars": chars}
    for name in ("top_k", "include_text", "requested_provider", "providers", "features", "urls", "routing_settings", "workspace"):
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


class GrounderWebSearchProvider(WebSearchProvider):
    """Grounder's search API as a Hermes web search and extract provider."""

    def __init__(self) -> None:
        # Each page a search returned and that search's query, oldest first, so an extract can be ranked against it. In memory only.
        self._found_by: dict[str, str] = {}
        # Each such page's title and the text the search showed of it, for a read of it that fails. In memory only.
        self._shown: dict[str, tuple[str, str]] = {}
        # The pages Grounder read, and each search's summary by its query: how the hooks tell Grounder's results from another backend's. In memory only.
        self._read: dict[str, bool] = {}
        self._searched: dict[str, dict[str, Any]] = {}
        # The searches of each turn not yet answered, by session and turn, and the turns whose model has read the notes.
        self._turns: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._noted: dict[tuple[str, str], bool] = {}
        # Each turn's results Grounder served, by page, with the number the model cites it by and its title and link.
        self._sources: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "grounder"

    @property
    def display_name(self) -> str:
        return "Grounder"

    def is_available(self) -> bool:
        """True with a search key or the sign-in client set; checks no network.

        Hermes turns web_search and web_extract off when no selected backend
        says it is ready, and offers a plugin no way to see its grant without a
        request. So the client id, the first step of signing in, stands for the
        sign-in; with no grant behind it a search says how to sign in.
        """
        return bool(_key() or str(get_secret(CLIENT_ENV) or "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Search Grounder for up to ``limit`` results (1 to ``MAX_RESULTS``), or read the pages a query names (``_read_part``)."""
        pages = list(dict.fromkeys(address.rstrip(".,;:!?)]}") for address in ADDRESS.findall(query)))[:MAX_PAGES]
        if pages:
            return self._read_part(query, pages)
        # Grounder fills each result's text with the passages that best match the query, within the budget.
        payload = {"query": query[:MAX_QUERY_CHARS],
                   "top_k": max(1, min(int(limit), MAX_RESULTS)),
                   "text_max_chars": DESCRIPTION_CHARS}
        body, error = _send(payload)
        if body is None:
            return _fail(error or "Grounder returned no answer")
        results = body.get("results") or []
        rows = [_row(r, i + 1) for i, r in enumerate(results[:max(1, int(limit))])]
        self._remember(query[:MAX_QUERY_CHARS], rows, _summary(body, len(rows)))
        # The count only: a query is the user's words, and a log line outlives the search.
        logger.info("Grounder search: %d results", len(rows))
        return {"success": True, "data": {"web": rows}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Read ``urls`` in one search, so it counts once: an entry per URL in their order, with an ``error`` on each page not read.

        Grounder keeps what in each page answers the question and at most ``EXTRACT_CHARS`` of it, to spare the model the rest, or as much as the
        model's ``char_limit`` asked for, up to ``MAX_EXTRACT_CHARS`` (``note_turn``). Hermes's ``format`` and other ``kwargs`` change nothing.
        """
        asked = [str(url) for url in urls]
        pages = list(dict.fromkeys(asked))[:MAX_PAGES]
        if not pages:
            return []
        chars = _LIMITS.pop(_session(), EXTRACT_CHARS)
        body, error = _send({"query": self._question(pages), "urls": pages, "top_k": len(pages),
                             "text_max_chars": chars, "features": ["page-text"]})
        if body is None:
            return [_page_error(url, error or "Grounder returned no answer") for url in asked]
        found = _pages_found(pages, body.get("results") or [])
        # The count only: an address says what the user is reading, and a log line outlives the search.
        logger.info("Grounder extract: %d of %d pages read", len(found), len(pages))
        with self._lock:
            shown = {page: self._shown[_page_key(page)] for page in pages if page not in found and _page_key(page) in self._shown}
            for page in [*found, *shown]:
                self._read[_page_key(page)] = True
            _bounded(self._read, REMEMBERED_PAGES)
        why = _unread(body)
        return [_page_shown(url, shown[url]) if url in shown else _page_row(url, found.get(url), why.get(_page_key(url), ""), chars) for url in asked]

    def _read_part(self, query: str, pages: list[str]) -> dict[str, Any]:
        """A ``web_search`` whose query holds pages' addresses: read those pages for the query's other words, in ``web_search``'s shape (GRO-691).

        Hermes's ``web_extract`` takes only addresses, and answers a repeated read from its own cache, so a model that needed another part of a page
        fetched it with its own tools. Hermes keys its search cache by the query, so a new question reaches Grounder. A query of addresses alone is
        ranked as an extract is, against the search that found the pages.
        """
        question = " ".join(ADDRESS.sub(" ", query).split())[:MAX_QUERY_CHARS] or self._question(pages)
        body, error = _send({"query": question, "urls": pages, "top_k": len(pages),
                             "text_max_chars": EXTRACT_CHARS, "features": ["page-text"]})
        if body is None:
            return _fail(error or "Grounder returned no answer")
        found = _pages_found(pages, body.get("results") or [])
        logger.info("Grounder read by search: %d of %d pages read", len(found), len(pages))
        with self._lock:
            shown = {page: self._shown[_page_key(page)] for page in pages if page not in found and _page_key(page) in self._shown}
            for page in [*found, *shown]:
                self._read[_page_key(page)] = True
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
        self._remember(query[:MAX_QUERY_CHARS], rows, _summary(body, len(rows)), found_by=False)
        return {"success": True, "data": {"web": rows}}

    def after_tool(self, tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", turn_id: str = "", **_: Any) -> Optional[str]:
        """Hermes's ``transform_tool_result``: note a search for the line under the answer, number each result Grounder served across the turn, and add
        the notes to the turn's first one.

        Hermes keeps only a page's url, title, content and error from an extract, so its number and the notes are added here, after that trim, and never
        inside a page's text.
        """
        if tool_name not in ("web_search", SEARCH_TOOL, "web_extract"):
            return None
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        data = data if isinstance(data, dict) else {}
        turn = (session_id, turn_id)
        # A host that names no turn cannot say which answer a result serves: positions stay the search's own, and every result gets the notes.
        named = all(turn)
        with self._lock:
            if tool_name == "web_extract":
                entries = data.get("results") if isinstance(data.get("results"), list) else []
                served = [entry for entry in entries if isinstance(entry, dict) and entry.get("content") and entry.get("url")
                          and _page_key(str(entry["url"])) in self._read]
            else:
                summary = self._searched.get(str((args or {}).get("query", ""))[:MAX_QUERY_CHARS])
                searched = summary is not None and bool(summary["sources"])
                if searched:
                    self._turns.setdefault(turn, []).append(summary)
                    _bounded(self._turns, REMEMBERED_TURNS)
                rows = (data.get("data") or {}).get("web") if isinstance(data.get("data"), dict) else None
                served = [row for row in rows if isinstance(row, dict) and row.get("url")] if searched and isinstance(rows, list) else []
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
        extra = {FILES_KEY: FILES_NOTE, CITE_KEY: CITE_REQUEST if named else CITE_REQUEST_LISTED} if notes else {}
        return json.dumps({**data, **extra}, indent=2, ensure_ascii=False)

    def after_answer(self, response_text: str = "", session_id: str = "", turn_id: str = "", **_: Any) -> Optional[str]:
        """Hermes's ``transform_llm_output``: end an answer with the Sources list of what it cites and, when it searched, Grounder's card line.
        Appends only, so Hermes keeps streaming the answer."""
        with self._lock:
            searches = self._turns.pop((session_id, turn_id), None)
            sources = self._sources.pop((session_id, turn_id), None)
        if not response_text:
            return None
        tail = [part for part in (_sources_list(response_text, sources or {}), _card_line(searches) if searches else "") if part]
        return f"{response_text.rstrip()}\n\n" + "\n\n".join(tail) if tail else None

    def search_tool(self, args: dict[str, Any], **_: Any) -> str:
        """The ``grounder_search`` tool: one search with every option Grounder's MCP search takes, answered in ``web_search``'s shape."""
        query = str(args.get("query") or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return json.dumps(_fail("grounder_search needs a query"))
        chars = EXTRACT_CHARS if args.get("urls") else DESCRIPTION_CHARS
        body, error = _send(_options(args, query, chars))
        if body is None:
            return json.dumps(_fail(error or "Grounder returned no answer"))
        rows = [_row(r, i + 1, chars) for i, r in enumerate(body.get("results") or [])]
        self._remember(query, rows, _summary(body, len(rows)))
        logger.info("Grounder search: %d results", len(rows))
        features = {"features": body["features"]} if body.get("features") else {}
        return json.dumps({"success": True, "data": {"web": rows}, **features}, indent=2, ensure_ascii=False)

    def list_providers_tool(self, args: dict[str, Any], **_: Any) -> str:
        """The ``grounder_list_providers`` tool: what Grounder's MCP ``list_providers`` says, from the public feature list. Sends no credential and is not a search."""
        request = urllib.request.Request(FEATURES_URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            return json.dumps(_fail(f"Could not reach Grounder: {error}"))
        return json.dumps({"success": True, "data": {"features": [_feature(f) for f in body.get("features") or [] if isinstance(f, dict)]}}, ensure_ascii=False)

    def _remember(self, query: str, rows: list[dict[str, Any]], summary: dict[str, Any], found_by: bool = True) -> None:
        """Note the query that found each of a search's pages, for the extract that may read them next, and the search's summary.
        A read by search names its pages rather than finding them, so it leaves the query that found them in place."""
        with self._lock:
            for row in rows if found_by else []:
                key = _page_key(row["url"])
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
        """What to rank the pages against: the web_search that found the most of them, else the user's latest message, else their addresses, which ask
        nothing."""
        with self._lock:
            found = [self._found_by.get(_page_key(page)) for page in pages]
        counts = Counter(query for query in found if query)
        if counts:
            return counts.most_common(1)[0][0]
        return _ASKS.get(_session()) or _addresses(pages)

    def get_setup_schema(self) -> dict[str, Any]:
        """The entry ``hermes tools`` shows. It asks for no key, since signing in needs none; the tag names both ways in."""
        return {
            "name": "Grounder", "badge": "account",
            "tag": "Licensed, live sources: many search providers in one call, "
                   f"one deduplicated list, and page reading. A search key in {KEY_ENV}, or sign in with `{SIGN_IN}`.",
            "env_vars": [],
        }
