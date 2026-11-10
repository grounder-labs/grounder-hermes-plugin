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

Select it with ``web.search_backend: grounder`` and ``web.extract_backend: grounder``.
A call uses the Grounder search key in ``GROUNDER_API_KEY`` when one is set, and otherwise the sign-in Hermes keeps
for the ``oauth:`` client in ``plugin.yaml`` (``hermes auth add grounder:grounder``):
Hermes signs in, stores and refreshes that grant and attaches its token to the
request, so this plugin never holds a token.
"""

from __future__ import annotations

import json
import logging
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
OAUTH_ID = "grounder"  # the id of the oauth: entry in plugin.yaml
SIGN_IN = f"hermes auth add grounder:{OAUTH_ID}"  # the plugin's key (its name), a colon, the entry's id
USER_AGENT = "grounder-hermes/0.4.1"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
KEY_ENV = "GROUNDER_API_KEY"
CLIENT_ENV = "GROUNDER_OAUTH_CLIENT_ID"  # set once, as the first step of signing in
MAX_RESULTS = 20  # most results one search asks Grounder for
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
DESCRIPTION_CHARS = 800  # a row's description, and the text Grounder is asked to fit it in (text_max_chars)
EXTRACT_CHARS = 6000  # the most text of one page web_extract returns, and what Grounder is asked to keep it within
MAX_PAGES = 10  # Grounder's limit on the pages one call reads; Hermes asks for at most 5
REMEMBERED_PAGES = 500  # how many pages the plugin remembers the web_search of, in memory only
TIMEOUT_S = 45  # one search, across every provider it reaches


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


def _description(result: dict[str, Any]) -> str:
    """The result's summary, else its text, on one line.

    Grounder sends the text as the passages that best match the query, already
    within ``DESCRIPTION_CHARS``, so this chooses nothing and cuts nothing of
    it. The cap is a guard for a response that ignored ``text_max_chars`` (an
    older Grounder, or a summary longer than the budget): a row never carries
    more than the budget.
    """
    text = " ".join(str(result.get("summary") or result.get("text") or "").split())
    if len(text) > DESCRIPTION_CHARS:
        text = text[:DESCRIPTION_CHARS - 1].rstrip() + "…"
    return text


def _send(payload: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One POST to Grounder: ``(body, None)``, or ``(None, error)`` when it failed. Never raises, and never retries: a second POST is a second search."""
    key = _key()
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


def _page_row(url: str, row: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Hermes's entry for one page, or its error when no result read it."""
    if row is None:
        return _page_error(url, "Grounder could not read this page, or its site does not allow it.")
    text = str(row.get("text") or "").strip()
    # A guard for a response that ignored ``text_max_chars``, as ``_description`` has for a row.
    if len(text) > EXTRACT_CHARS:
        text = text[:EXTRACT_CHARS - 1].rstrip() + "…"
    return {"url": url, "title": str(row.get("title", "")), "content": text, "raw_content": text}


def _page_error(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error}


class GrounderWebSearchProvider(WebSearchProvider):
    """Grounder's search API as a Hermes web search and extract provider."""

    def __init__(self) -> None:
        # Each page a search returned and that search's query, oldest first, so an extract can be ranked against it. In memory only.
        self._found_by: dict[str, str] = {}
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
        """Search Grounder for up to ``limit`` results (1 to ``MAX_RESULTS``)."""
        # Grounder fills each result's text with the passages that best match the query, within the budget.
        payload = {"query": query[:MAX_QUERY_CHARS],
                   "top_k": max(1, min(int(limit), MAX_RESULTS)),
                   "text_max_chars": DESCRIPTION_CHARS}
        body, error = _send(payload)
        if body is None:
            return _fail(error or "Grounder returned no answer")
        results = body.get("results") or []
        rows = [
            {"title": str(r.get("title", "")), "url": str(r.get("url", "")),
             "description": _description(r), "position": i + 1}
            for i, r in enumerate(results[:max(1, int(limit))])
        ]
        self._remember(query[:MAX_QUERY_CHARS], rows)
        # The count only: a query is the user's words, and a log line outlives the search.
        logger.info("Grounder search: %d results", len(rows))
        return {"success": True, "data": {"web": rows}}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Read ``urls`` in one search, so it counts once: an entry per URL in their order, with an ``error`` on each page not read.

        Grounder keeps what in each page answers the question and at most ``EXTRACT_CHARS`` of it, to spare the model the rest. Hermes's
        ``format`` and other ``kwargs`` change nothing.
        """
        asked = [str(url) for url in urls]
        pages = list(dict.fromkeys(asked))[:MAX_PAGES]
        if not pages:
            return []
        body, error = _send({"query": self._question(pages), "urls": pages, "top_k": len(pages),
                             "text_max_chars": EXTRACT_CHARS, "features": ["page-text"]})
        if body is None:
            return [_page_error(url, error or "Grounder returned no answer") for url in asked]
        found = _pages_found(pages, body.get("results") or [])
        # The count only: an address says what the user is reading, and a log line outlives the search.
        logger.info("Grounder extract: %d of %d pages read", len(found), len(pages))
        return [_page_row(url, found.get(url)) for url in asked]

    def _remember(self, query: str, rows: list[dict[str, Any]]) -> None:
        """Note the query that found each of a search's pages, for the extract that may read them next."""
        with self._lock:
            for row in rows:
                key = _page_key(row["url"])
                self._found_by.pop(key, None)  # a page found again moves to the newest place
                self._found_by[key] = query
            while len(self._found_by) > REMEMBERED_PAGES:
                del self._found_by[next(iter(self._found_by))]

    def _question(self, pages: list[str]) -> str:
        """What to rank the pages against: the web_search that found the most of them, else their addresses, which ask nothing."""
        with self._lock:
            found = [self._found_by.get(_page_key(page)) for page in pages]
        counts = Counter(query for query in found if query)
        return counts.most_common(1)[0][0] if counts else _addresses(pages)

    def get_setup_schema(self) -> dict[str, Any]:
        """The entry ``hermes tools`` shows. It asks for no key, since signing in needs none; the tag names both ways in."""
        return {
            "name": "Grounder", "badge": "account",
            "tag": "Licensed, live sources: many search providers in one call, "
                   f"one deduplicated list, and page reading. A search key in {KEY_ENV}, or sign in with `{SIGN_IN}`.",
            "env_vars": [],
        }
