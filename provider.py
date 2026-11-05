# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder as a Hermes web search backend.

Each ``web_search`` call is one ``POST /v1/search`` to Grounder, which reaches the
providers the query needs and returns one deduplicated list of licensed results.
The provider maps those results to Hermes's rows (title, URL, description,
position) and reports every failure as a ``{"success": False, "error": ...}``
result, never an exception. Search only: pair it with any extract backend for
``web_extract``.

Select it with ``web.search_backend: grounder``. Credentials come from
``hermes grounder login`` (see ``auth``) or, ahead of that, an existing Grounder
search key in ``GROUNDER_API_KEY``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from agent.web_search_provider import WebSearchProvider, get_provider_env

from . import auth

logger = logging.getLogger(__name__)

KEY_ENV = "GROUNDER_API_KEY"
MAX_RESULTS = 20  # most results one search asks Grounder for
MAX_QUERY_CHARS = 2000  # Grounder's limit on a query
DESCRIPTION_CHARS = 800  # a row's description, and the text Grounder is asked to fit it in (text_max_chars)
TIMEOUT_S = 45  # one search, across every provider it reaches


def _fail(error: str) -> dict[str, Any]:
    """The failure result Hermes shows the model in place of rows."""
    return {"success": False, "error": error}


def _post_search(token: str, query: str, limit: int) -> tuple[int, Any]:
    """Send one search; return the HTTP status and the parsed JSON body (None when an error body is not JSON)."""
    # Grounder fills each result's text with the passages that best match the query, within the budget.
    body = json.dumps({"query": query[:MAX_QUERY_CHARS],
                       "top_k": max(1, min(int(limit), MAX_RESULTS)),
                       "text_max_chars": DESCRIPTION_CHARS})
    request = urllib.request.Request(
        f"{auth.ORIGIN}/v1/search", body.encode(),
        {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": auth.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, None


def _error_message(status: int, body: Any) -> str:
    """A one-line message for a failed search, with Grounder's own reason when it gave one."""
    detail: Optional[str] = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = body["error"].get("message")
    if status == 401:
        return ("Grounder rejected the credentials. "
                "Run `hermes grounder login`.")
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
        """True with a key in the environment or a saved sign-in; checks no network."""
        return bool(get_provider_env(KEY_ENV)) or auth.token_path().is_file()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Search Grounder for up to ``limit`` results (1 to ``MAX_RESULTS``).

        A signed-in search whose access token is refused is refreshed and sent
        once more; a key from the environment is never refreshed.
        """
        key = get_provider_env(KEY_ENV)
        try:
            token = key or auth.access_token()
            status, body = _post_search(token, query, limit)
            if status == 401 and not key:
                status, body = _post_search(auth.refresh(token), query, limit)
        except auth.AuthError as error:
            return _fail(str(error))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            logger.warning("Grounder request error: %s", error)
            return _fail(f"Could not reach Grounder: {error}")
        except ValueError:
            return _fail("Could not parse the Grounder response as JSON")
        if status != 200 or not isinstance(body, dict):
            return _fail(_error_message(status, body))
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
        """The entry ``hermes tools`` shows: sign-in, so no key to ask for."""
        return {
            "name": "Grounder", "badge": "account",
            "tag": "Licensed, live sources: many search providers in one call, "
                   "one deduplicated list. Search only; sign in with "
                   "`hermes grounder login`.",
            "env_vars": [],
        }
