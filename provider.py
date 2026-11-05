# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder as a Hermes web search backend.

Each ``web_search`` call is one ``POST /v1/search`` to Grounder, which reaches the
providers the query needs and returns one deduplicated list of licensed results.
The provider maps those results to Hermes's rows (title, URL, description,
position) and reports every failure as a ``{"success": False, "error": ...}``
result, never an exception. Search only: pair it with any extract backend for
``web_extract``.

Select it with ``web.search_backend: grounder``. A search uses the Grounder search
key in ``GROUNDER_API_KEY`` when one is set, and otherwise the sign-in Hermes keeps
for the ``oauth:`` client in ``plugin.yaml`` (``hermes auth add grounder:grounder``):
Hermes signs in, stores and refreshes that grant and attaches its token to the
request, so this plugin never holds a token.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

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
USER_AGENT = "grounder-hermes/0.2.0"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
KEY_ENV = "GROUNDER_API_KEY"
MAX_RESULTS = 20  # most results one search asks Grounder for
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
DESCRIPTION_CHARS = 800  # a row's description, and the text Grounder is asked to fit it in (text_max_chars)
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


class GrounderWebSearchProvider(WebSearchProvider):
    """Grounder's search API as a search-only Hermes web provider."""

    @property
    def name(self) -> str:
        return "grounder"

    @property
    def display_name(self) -> str:
        return "Grounder"

    def is_available(self) -> bool:
        """True with a search key; checks no network.

        A sign-in alone does not count: Hermes offers a plugin no way to see
        its grant without a request, so a signed-in user selects Grounder
        (``web.search_backend: grounder``), which Hermes honours either way.
        """
        return bool(_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Search Grounder for up to ``limit`` results (1 to ``MAX_RESULTS``)."""
        # Grounder fills each result's text with the passages that best match the query, within the budget.
        payload = {"query": query[:MAX_QUERY_CHARS],
                   "top_k": max(1, min(int(limit), MAX_RESULTS)),
                   "text_max_chars": DESCRIPTION_CHARS}
        key = _key()
        try:
            status, raw = _post_with_key(key, payload) if key else _post_signed_in(payload)
        except ProviderNotSignedIn:
            return _fail(f"Not signed in to Grounder. Run `{SIGN_IN}`.")
        except AuthError as error:
            return _fail(f"Grounder sign-in failed: {error}")
        except PermissionError as error:
            return _fail(f"Hermes refused the Grounder sign-in: {error}")
        except (httpx.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            logger.warning("Grounder request error: %s", error)
            return _fail(f"Could not reach Grounder: {error}")
        try:
            body = json.loads(raw)
        except ValueError:
            if status == 200:
                return _fail("Could not parse the Grounder response as JSON")
            body = None
        if status != 200 or not isinstance(body, dict):
            return _fail(_error_message(status, body, signed_in=not key))
        results = body.get("results") or []
        rows = [
            {"title": str(r.get("title", "")), "url": str(r.get("url", "")),
             "description": _description(r), "position": i + 1}
            for i, r in enumerate(results[:max(1, int(limit))])
        ]
        # The count only: a query is the user's words, and a log line outlives the search.
        logger.info("Grounder search: %d results", len(rows))
        return {"success": True, "data": {"web": rows}}

    def get_setup_schema(self) -> dict[str, Any]:
        """The entry ``hermes tools`` shows. It asks for no key, since signing in needs none; the tag names both ways in."""
        return {
            "name": "Grounder", "badge": "account",
            "tag": "Licensed, live sources: many search providers in one call, "
                   f"one deduplicated list. Search only; a search key in {KEY_ENV}, or sign in with `{SIGN_IN}`.",
            "env_vars": [],
        }
