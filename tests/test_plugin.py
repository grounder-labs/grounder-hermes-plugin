# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from conftest import PLUGIN_DIR, manifest, yaml

RESULT = {"url": "https://example.com/a", "title": "A", "text": "first  passage\n text",
          "rank": 1, "provenance": {"found_by": ["brave"]}}
SIGN_IN = "hermes auth add grounder:grounder"


def _searches(stub):
    return [r for r in stub.requests if r["path"] == "/v1/search"]


def test_the_backend_is_named_and_ready_with_no_account(plugin):
    provider = plugin.GrounderWebSearchProvider()
    assert provider.name == "grounder" and provider.display_name == "Grounder"
    assert provider.supports_search() and provider.supports_extract()
    assert provider.is_available()  # a search needs no account, so Hermes keeps web_search and web_extract on


def test_with_no_key_and_no_sign_in_a_search_is_a_free_search_that_carries_no_credential(plugin, stub, free):
    stub.search_body = {"query": "q", "results": [RESULT]}
    assert plugin.GrounderWebSearchProvider().search("q")["success"]
    assert [call["provider"] for call in free.calls] == ["grounder"]  # signing in was tried first, and Hermes had no grant
    request = _searches(stub)[0]
    assert not {name.lower() for name in request["headers"]} & {"authorization", "cookie"}
    assert json.loads(request["body"]) == {"query": "q", "top_k": 5}  # no mode, and no option that cuts what Grounder returns
    assert request["headers"]["User-Agent"] == plugin.provider.USER_AGENT


def test_a_page_read_with_no_key_and_no_sign_in_is_free_too(plugin, stub, free):
    stub.search_body = {"results": [{**RESULT, "url": "https://example.com/a", "text": "page text"}]}
    rows = plugin.GrounderWebSearchProvider().extract(["https://example.com/a"])
    assert rows[0]["content"] == "page text" and "error" not in rows[0]
    assert "Authorization" not in _searches(stub)[0]["headers"]


def test_a_free_search_sends_neither_the_chosen_server_nor_the_mode(plugin, stub, free, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"web": {"grounder": {"preset": "deep", "server": "provider:exa"}}}))
    plugin.GrounderWebSearchProvider().search("q")
    assert json.loads(_searches(stub)[0]["body"]) == {"query": "q", "top_k": 5}


def test_a_free_search_drops_the_options_grounder_refuses_a_caller_with_no_account_and_says_so(plugin, stub, free):
    # Grounder answers a caller with no account 400 for a profile, routing settings or a workspace: nothing is sent that it would refuse, so no round trip is wasted.
    stub.search_body = {"query": "q", "results": [RESULT]}
    provider = plugin.GrounderWebSearchProvider()
    out = json.loads(provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"providers": {"exa": {"use": "always"}}},
                                            "workspace": "team", "top_k": 3}))
    sent = json.loads(_searches(stub)[0]["body"])
    assert len(_searches(stub)) == 1 and sent["top_k"] == 3
    assert not {"profile", "routing_settings", "workspace"} & set(sent)
    assert out["success"] is True and out["data"]["web"] and "profile, routing_settings, workspace were not sent" in out["note"] and SIGN_IN in out["note"]
    # One that asked for none gets no note, and a search with a key sends all three.
    assert "note" not in json.loads(provider.search_tool({"query": "q"}))


def test_a_signed_in_search_keeps_the_options_a_free_one_drops(plugin, stub, hermes):
    hermes.token = "t"
    out = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q", "profile": "fast", "workspace": "team"}))
    sent = json.loads(_searches(stub)[0]["body"])
    assert sent["profile"] == "fast" and sent["workspace"] == "team" and "note" not in out


def test_a_free_search_that_reaches_a_limit_says_where_to_sign_up_and_add_credit(plugin, stub, free):
    links = {"signup_url": "https://grounder.net/account", "billing_url": "https://grounder.net/account/billing"}
    provider = plugin.GrounderWebSearchProvider()
    stub.search_status = 429
    stub.search_body = {"error": {"code": "daily_limit_reached", "message": "the API's own words", "details": {"daily_limit": 50, **links}}}
    said = ("You've used Grounder's free searches. Sign up at https://grounder.net/account, then add credit at https://grounder.net/account/billing. "
            f"Then sign in with `{SIGN_IN}`, or set a search key in `GROUNDER_API_KEY`.")
    # Hermes serves a FAILED search from its keyless ring of other vendors, which would hide this: it is a successful result that carries the words (GRO-832).
    daily = provider.search("q")
    assert daily["success"] is True and daily["data"][plugin.provider.NOTICE_KEY] == "act"
    [row] = daily["data"]["web"]
    assert said in row["description"] and row["url"] == "https://grounder.net/account" and row["description"].startswith("Grounder did not run this search.")
    stub.search_status = 503
    stub.search_body = {"error": {"code": "capacity_reached", "message": "the API's own words", "details": links}}
    capacity = provider.search("q")["data"]["web"][0]["description"]
    assert "Grounder's free searches have reached their limit for now. Sign up at https://grounder.net/account, then add credit at " in capacity
    # The plugin names no limit of its own: a number in these words would be one Grounder can change.
    assert not re.search(r"\d", row["description"].replace(links["signup_url"], "").replace(links["billing_url"], "")) and not re.search(r"\d", capacity.replace(links["signup_url"], "").replace(links["billing_url"], ""))
    stub.search_status = 200


def test_a_per_minute_limit_with_no_links_keeps_the_apis_words(plugin, stub, free):
    stub.search_status = 429
    stub.search_body = {"error": {"code": "rate_limited", "message": "10 a minute"}}
    assert plugin.GrounderWebSearchProvider().search("q") == {"success": False, "error": "Grounder rate limit reached: 10 a minute"}


def test_a_limit_on_a_search_with_a_key_is_not_a_free_search_limit(plugin, stub, keyed):
    # With a key the search is not free, so Grounder's sign-up words are not its answer to it: its own words, the account's, are.
    stub.search_status = 429
    stub.search_body = {"error": {"code": "daily_limit_reached", "message": "add credit", "details": {"signup_url": "https://grounder.net/account"}}}
    [row] = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    assert row["description"] == "Grounder did not run this search. add credit Tell the user this; the same search fails again until they have."
    assert "Sign up" not in row["description"]


def test_signed_in_search_goes_through_hermes_which_attaches_the_token(plugin, stub, hermes):
    # The plugin names the provider, the address and the body; the Authorization header is Hermes's to add, never the plugin's.
    hermes.token = "hermes-held-token"
    stub.search_body = {"query": "q", "results": [
        RESULT, {**RESULT, "url": "https://example.com/b", "title": "B",
                 "summary": "short", "text": "x" * 5000}]}
    result = plugin.GrounderWebSearchProvider().search("offshore wind", limit=50)
    assert result == {"success": True, "data": {"web": [
        {"title": "A", "url": "https://example.com/a",
         "description": "first passage text", "position": 1},
        {"title": "B", "url": "https://example.com/b",
         "description": "short", "position": 2}]}}
    call = hermes.calls[0]
    assert (call["provider"], call["method"], call["url"]) == ("grounder", "POST", "https://api.grounder.net/v1/search")
    assert call["json"] == {"query": "offshore wind", "top_k": 50, "mode": "optimized"}  # the limit Hermes asked for, within Grounder's 100
    assert not {name.lower() for name in call["headers"]} & {"authorization", "cookie"}
    assert _searches(stub)[0]["headers"]["Authorization"] == "Bearer hermes-held-token"


def test_the_plugin_keeps_no_credential_of_its_own(plugin, stub, hermes, tmp_path):
    # Admission rule 11: Hermes stores and refreshes the grant; nothing is written under the Hermes home.
    hermes.token = "hermes-held-token"
    assert plugin.GrounderWebSearchProvider().search("q")["success"]
    assert list(tmp_path.iterdir()) == []
    assert not hasattr(plugin, "auth")
    # Hermes's admission check warns on any .py file in the plugin that names the token URL: the grant's endpoints are Hermes's alone.
    token_url = manifest()["oauth"][0]["token_url"]
    for source in PLUGIN_DIR.rglob("*.py"):
        assert token_url not in source.read_text(), source.name
    for source in PLUGIN_DIR.glob("*.py"):
        text = source.read_text()  # the public client's address, /oauth/clients/, is an identifier and no endpoint
        assert "/oauth/token" not in text and "/oauth/authorize" not in text, source.name


def test_manifest_declares_the_client_hermes_signs_in_to(plugin):
    data = manifest()
    entry, = data["oauth"]
    origin = plugin.provider.ORIGIN
    assert entry["id"] == plugin.provider.OAUTH_ID
    assert entry["authorize_url"] == f"{origin}/oauth/authorize"
    assert entry["token_url"] == f"{origin}/oauth/token"
    assert entry["scopes"] == ["search"]
    assert entry["authorize_params"] == {"resource": f"{origin}/v1/search"}
    # The bearer may go only where the plugin sends searches.
    assert entry["hosts"] == [urlsplit(plugin.provider.SEARCH_URL).hostname]
    # Neither way in is required of the other: a key user sets no client, a signed-in user no key, so nothing is in requires_env.
    assert "requires_env" not in data
    optional = {e["name"]: e for e in data["optional_env"]}
    assert set(optional) == {plugin.provider.KEY_ENV, entry["client_id_env"]}
    assert optional[plugin.provider.KEY_ENV]["secret"] is True
    assert not optional[entry["client_id_env"]].get("secret")
    assert "https://api.grounder.net/oauth/clients/hermes-agent" in optional[entry["client_id_env"]]["description"]
    assert data["requires_hermes"] == ">=0.22.3"
    assert data["name"] == "grounder" and plugin.provider.SIGN_IN == f"hermes auth add {data['name']}:{entry['id']}"


def test_logs_the_result_count_and_never_the_query(plugin, stub, hermes, caplog):
    # A query is the user's words; a log line outlives the search. Every level, the failing paths too.
    hermes.token = "t"
    stub.search_body = {"results": [RESULT]}
    secret = "my unreleased merger with acme corp"
    with caplog.at_level("DEBUG"):
        assert plugin.GrounderWebSearchProvider().search(secret)["success"]
        stub.search_status = 500
        stub.search_body = {"error": {"code": "internal_error", "message": "down"}}
        assert not plugin.GrounderWebSearchProvider().search(secret)["success"]
        hermes.raises = httpx.ConnectError("connection refused")
        assert not plugin.GrounderWebSearchProvider().search(secret)["success"]
    assert any("1 results" in record.getMessage() for record in caplog.records)
    assert not [r.getMessage() for r in caplog.records if "merger" in r.getMessage() or "acme" in r.getMessage()]
    assert not [r for r in caplog.records if secret in str(r.args) or secret in r.getMessage()]


def _api_shapes():
    # What POST /v1/search answers for this query at text_max_chars 800, recorded from the passage code the API runs
    # (workers/router/src/lib/passages.ts): the answer at the head, deep in the text, nowhere, a text shorter than the budget, and one of 100,000 characters.
    return json.loads((Path(__file__).parent / "api_shapes.json").read_text())


def _bodies(stub):
    return [json.loads(request["body"]) for request in _searches(stub)]


CUTTING = ("text_max_chars", "rerank", "passage")  # the names of any option that would have Grounder or the plugin cut or reorder what a provider returned


def test_no_request_carries_an_option_that_cuts_or_reranks_what_grounder_returns(plugin, stub, hermes):
    # Every mode returns exactly what the provider returns: not one request, of any kind, asks for a character budget, a passage or a rerank.
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A, text="x" * 50000)]}
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search("q", limit=100)
    provider.search_tool({"query": "q"})
    provider.search_tool({"query": "q", "urls": [PAGE_A]})
    provider.extract([PAGE_A])
    provider.search(f"{PAGE_A} more")
    bodies = _bodies(stub)
    assert len(bodies) == 6
    for body in bodies:
        assert not [key for key in body if any(word in key.lower() for word in CUTTING)], body
    assert "text_max_chars" not in json.dumps(bodies)


def test_rows_carry_the_text_grounder_returned_as_it_came_on_one_line(plugin, stub, hermes):
    # The answer early, the answer late, no match, very short, very long: every row is what the API sent (white space aside), none cut or re-chosen here.
    hermes.token = "t"
    shapes = _api_shapes()
    names = ["early", "late", "none", "short", "long"]
    stub.search_body = {"query": shapes["query"], "results": [
        {**RESULT, "url": f"https://example.com/{name}", "title": name, "rank": i + 1, "text": shapes[name]["text"]}
        for i, name in enumerate(names)]}
    rows = plugin.GrounderWebSearchProvider().search(shapes["query"], limit=5)["data"]["web"]
    assert [row["title"] for row in rows] == names
    for row, name in zip(rows, names):
        sent = shapes[name]["text"]
        assert row["description"] == " ".join(sent.split()), name
        assert not row["description"].endswith("…") or sent.endswith("…"), name
    by_name = {row["title"]: row["description"] for row in rows}
    assert "The capital of Freedonia is Fredville" in by_name["early"] and "The capital of Freedonia is Fredville" in by_name["late"]
    assert " … " in by_name["late"]  # a passage join Grounder put there
    assert by_name["short"] == "Fredville is the capital of Freedonia."


def test_a_rows_description_is_never_cut_however_long_and_loses_no_word(plugin, stub, hermes):
    hermes.token = "t"
    long_text = "word\n\t" * 40000 + "last"  # 240,000 characters of text
    stub.search_body = {"results": [{**RESULT, "text": long_text},
                                    {**RESULT, "url": "https://example.com/s", "summary": "s" * 20000, "text": "t"},
                                    {**RESULT, "url": "https://example.com/e", "text": "word " * 159 + "words"}]}
    rows = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    assert rows[0]["description"] == " ".join(long_text.split()) and len(rows[0]["description"]) > 200000 and rows[0]["description"].endswith("last")
    assert rows[1]["description"] == "s" * 20000 and not rows[1]["description"].endswith("…")
    assert rows[2]["description"] == "word " * 159 + "words"
    for row in rows:
        assert "\n" not in row["description"] and "\t" not in row["description"]


def test_top_k_goes_up_to_100_the_range_grounder_and_hermes_both_allow(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [{**RESULT, "url": f"https://example.com/{i}"} for i in range(120)]}
    provider = plugin.GrounderWebSearchProvider()
    assert plugin.provider.MAX_RESULTS == 100
    assert len(provider.search("q", limit=100)["data"]["web"]) == 100  # the rows Hermes asked for, no more
    assert len(provider.search("q", limit=30)["data"]["web"]) == 30
    provider.search("q", limit=150)  # a limit past Grounder's range is held to it
    provider.search("q", limit=1)
    assert [body["top_k"] for body in _bodies(stub)] == [100, 30, 100, 1]


def test_query_is_cut_to_2000_characters_and_the_limit_kept_in_range(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [RESULT, {**RESULT, "url": "https://example.com/b"}]}
    provider = plugin.GrounderWebSearchProvider()
    rows = provider.search("é" * 2500, limit=0)["data"]["web"]
    assert len(rows) == 1
    assert json.loads(_searches(stub)[0]["body"]) == {"query": "é" * 2000, "top_k": 1, "mode": "optimized"}
    provider.search("q")
    assert json.loads(_searches(stub)[1]["body"])["top_k"] == 5


def test_user_agent_names_the_plugin_at_its_manifest_version_on_both_paths(plugin, stub, hermes, monkeypatch):
    # Grounder names these searches Hermes Agent by this user agent; the version follows plugin.yaml.
    version = re.search(r"^version: (\S+)$", (PLUGIN_DIR / "plugin.yaml").read_text(), re.M).group(1)
    assert plugin.provider.USER_AGENT == f"grounder-hermes/{version}"
    hermes.token = "t"
    plugin.GrounderWebSearchProvider().search("q")
    monkeypatch.setenv("GROUNDER_API_KEY", "user-key")
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", f"{stub.origin}/v1/search")
    plugin.GrounderWebSearchProvider().search("q")
    assert [r["headers"]["User-Agent"] for r in _searches(stub)] == [plugin.provider.USER_AGENT] * 2


def test_key_in_environment_wins_and_goes_without_hermes(plugin, stub, hermes, keyed):
    hermes.token = "hermes-held-token"
    provider = plugin.GrounderWebSearchProvider()
    assert provider.is_available()
    stub.search_body = {"results": [RESULT]}
    assert provider.search("q")["success"]
    assert hermes.calls == []
    request = _searches(stub)[0]
    assert request["headers"]["Authorization"] == f"Bearer {keyed}"
    assert json.loads(request["body"]) == {"query": "q", "top_k": 5, "mode": "optimized"}


def _said(result):
    """The words of a result that is Grounder's message to the user in place of rows."""
    assert result["success"] is True and result["data"][NOTICE] == "act", result
    return result["data"]["web"][0]["description"]


NOTICE = "grounder_notice"


def test_a_refused_key_a_refused_sign_in_and_a_refused_free_search_each_say_their_own_true_thing(plugin, stub, hermes, monkeypatch):
    hermes.token = "revoked-token"
    assert f"Grounder rejected the sign-in. Run `{SIGN_IN}`." in _said(plugin.GrounderWebSearchProvider().search("q"))
    monkeypatch.setenv("GROUNDER_API_KEY", "revoked-key")
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", f"{stub.origin}/v1/search")
    key = _said(plugin.GrounderWebSearchProvider().search("q"))
    assert "Grounder rejected the search key in `GROUNDER_API_KEY`." in key and "grounder.net/account/keys" in key
    assert len(_searches(stub)) == 2 and len(hermes.calls) == 1
    # A free search carries no key: it must never be told it sent one (seen live on 0.5.14, which said "rejected the search key in GROUNDER_API_KEY" with none set).
    monkeypatch.delenv("GROUNDER_API_KEY")
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", f"{plugin.provider.ORIGIN}/v1/search")
    hermes.token = None
    stub.search_status, stub.search_body = 401, {"error": {"code": "unauthorized", "message": "expired"}}
    free = _said(plugin.GrounderWebSearchProvider().search("q"))
    assert "GROUNDER_API_KEY`." in free and "rejected the search key" not in free and "rejected the sign-in" not in free and SIGN_IN in free
    # The same words reach a page read, as the page's content with no error, so Hermes serves nothing in its place.
    [page] = plugin.GrounderWebSearchProvider().extract(["https://example.com/a"])
    assert "error" not in page and "no account" in page["content"] and page["url"] == plugin.provider.GROUNDER_SITE
    stub.search_status = 200


def test_what_hermes_raises_becomes_a_failure_not_an_exception(plugin, stub, hermes):
    provider = plugin.GrounderWebSearchProvider()
    hermes.raises = plugin.provider.AuthError(
        "grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder",
        code="oauth_client_id_missing")
    # A sign-in the user must fix is Grounder's message to them; a refresh that failed on the network is as transient as a timeout.
    assert "Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set." in _said(provider.search("q"))
    hermes.raises = PermissionError("Refusing to send the grounder sign-in to elsewhere.example")
    assert "Hermes refused the Grounder sign-in: Refusing to send the grounder sign-in to elsewhere.example" in _said(provider.search("q"))
    hermes.raises = plugin.provider.AuthError("refresh failed: timed out", provider="grounder:grounder", code="oauth_refresh_failed")
    assert provider.search("q") == {"success": False, "error": "Grounder sign-in failed: refresh failed: timed out"}
    hermes.raises = httpx.ConnectTimeout("timed out")
    assert provider.search("q") == {"success": False, "error": "Could not reach Grounder: timed out"}
    assert stub.requests == []


def test_http_errors_become_readable_messages(plugin, stub, hermes):
    hermes.token = "t"
    provider = plugin.GrounderWebSearchProvider()
    stub.search_status = 429
    stub.search_body = {"error": {"code": "rate_limited", "message": "60 per minute"}}
    assert provider.search("q") == {
        "success": False, "error": "Grounder rate limit reached: 60 per minute"}
    stub.search_body = {}
    assert provider.search("q")["error"] == "Grounder rate limit reached: try again later"
    stub.search_status = 500
    stub.search_body = {"error": {"code": "internal", "message": "boom"}}
    assert provider.search("q") == {
        "success": False, "error": "Grounder returned HTTP 500: boom"}
    stub.search_status = 502
    stub.search_body = b"<html>Bad gateway</html>"
    assert provider.search("q") == {"success": False, "error": "Grounder returned HTTP 502"}
    stub.search_status = 200
    assert provider.search("q") == {
        "success": False, "error": "Could not parse the Grounder response as JSON"}


def test_unreachable_api_with_a_key_is_reported_not_raised(plugin, stub, keyed, monkeypatch):
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", "http://127.0.0.1:9/v1/search")
    result = plugin.GrounderWebSearchProvider().search("q")
    assert not result["success"] and result["error"].startswith("Could not reach Grounder")


def test_register_adds_the_backend_and_no_command(plugin):
    calls = {}

    class Context:
        def register_web_search_provider(self, provider):
            calls["provider"] = provider

        def register_hook(self, name, callback):
            calls.setdefault("hooks", {})[name] = callback

        def register_cli_command(self, **kwargs):
            calls["command"] = kwargs

    plugin.register(Context())
    assert calls["provider"].name == "grounder"
    assert "command" not in calls
    assert calls["hooks"] == {"transform_tool_result": calls["provider"].after_tool, "transform_llm_output": calls["provider"].after_answer, "pre_tool_call": plugin.provider.note_turn, "post_api_request": plugin.provider.note_model,
                              "pre_llm_call": plugin.provider.note_message}


def test_setup_entry_asks_for_no_key_and_names_both_ways_in(plugin):
    entry = plugin.GrounderWebSearchProvider().get_setup_schema()
    assert entry["name"] == "Grounder" and entry["badge"] == "account"
    assert entry["env_vars"] == []
    assert "`hermes auth add grounder:grounder`" in entry["tag"] and "GROUNDER_API_KEY" in entry["tag"]
    assert "Search only" not in entry["tag"] and "page reading" in entry["tag"]


# --- web_extract ---------------------------------------------------------------------------------------------------

PAGE_A, PAGE_B, PAGE_C = "https://example.com/a", "https://example.com/b", "https://example.com/c"


def _page(url, text="page text", title="T"):
    return {"url": url, "title": title, "text": text, "rank": 1, "provenance": {"found_by": ["reader"]}}


def test_extract_is_one_search_for_the_pages_with_no_question_when_no_search_found_them(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_B), _page(PAGE_A)]}
    rows = plugin.GrounderWebSearchProvider().extract([PAGE_A, PAGE_B], format="markdown", include_raw=True, max_chars=99)
    assert len(_searches(stub)) == 1
    assert json.loads(_searches(stub)[0]["body"]) == {"query": f"{PAGE_A} {PAGE_B}", "urls": [PAGE_A, PAGE_B], "top_k": 2, "features": ["page-text"]}  # no mode, no budget
    # In the order asked, whatever order Grounder answered in.
    assert rows == [{"url": PAGE_A, "title": "T", "content": "page text", "raw_content": "page text"},
                    {"url": PAGE_B, "title": "T", "content": "page text", "raw_content": "page text"}]


def test_extract_of_a_page_no_search_found_is_read_for_the_users_latest_message(plugin, stub, hermes, monkeypatch):
    provider_module = sys.modules[plugin.GrounderWebSearchProvider.__module__]
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A)]}
    monkeypatch.setenv("HERMES_SESSION_ID", "session-ask")
    assert provider_module.note_message(session_id="session-ask", user_message="  How many  books\nsince 2020? ") is None
    plugin.GrounderWebSearchProvider().extract([PAGE_A])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "How many books since 2020?"
    # Content parts keep their text; another session's message is never used.
    provider_module.note_message(session_id="session-ask", user_message=[{"type": "text", "text": "Which table?"}, {"type": "image_url", "image_url": {}}])
    provider_module.note_message(session_id="other", user_message="not this one")
    plugin.GrounderWebSearchProvider().extract([PAGE_A])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "Which table?"
    monkeypatch.setenv("HERMES_SESSION_ID", "session-silent")
    plugin.GrounderWebSearchProvider().extract([PAGE_A])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == PAGE_A


def test_extract_returns_a_page_whole_whatever_char_limit_the_model_asked_for(plugin, stub, hermes, monkeypatch):
    # Hermes applies a model's char_limit to the whole page itself: the plugin keeps none of it, asks Grounder for no budget and cuts nothing.
    provider_module = sys.modules[plugin.GrounderWebSearchProvider.__module__]
    hermes.token = "t"
    whole = "x" * 200000
    stub.search_body = {"results": [{**_page(PAGE_A), "text": f"  {whole}\n"}]}
    monkeypatch.setenv("HERMES_SESSION_ID", "session-limit")
    for limit in (None, 20000, 60000, "lots"):
        provider_module.note_turn(session_id="session-limit", tool_name="web_extract", args={"urls": [PAGE_A], **({"char_limit": limit} if limit else {})})
        page = plugin.GrounderWebSearchProvider().extract([PAGE_A])[0]
        assert page["content"] == page["raw_content"] == whole  # stripped, and nothing more
        assert "text_max_chars" not in json.loads(_searches(stub)[-1]["body"])
    assert not hasattr(provider_module, "_LIMITS") and not hasattr(provider_module, "EXTRACT_CHARS")


def test_extract_ranks_pages_against_the_search_that_found_them(plugin, stub, hermes):
    hermes.token = "t"
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = {"results": [{**RESULT, "url": PAGE_A}, {**RESULT, "url": PAGE_B}]}
    provider.search("offshore wind costs")
    stub.search_body = {"results": [{**RESULT, "url": PAGE_C}]}
    provider.search("something else")
    stub.search_body = {"results": [_page(PAGE_A)]}
    # Two of the three pages came from the first search: its query wins, and a page never found by a search takes nothing from it.
    provider.extract(["https://www.example.com/a/?utm_source=x", PAGE_B, PAGE_C, "https://other.example/z"])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "offshore wind costs"


def test_remembered_searches_are_bounded_and_leave_nothing_on_disk(plugin, stub, hermes, tmp_path):
    hermes.token = "t"
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = {"results": [{**RESULT, "url": f"https://example.com/{i}"} for i in range(5)]}
    for turn in range(120):
        stub.search_body = {"results": [{**RESULT, "url": f"https://example.com/{turn}/{i}"} for i in range(5)]}
        provider.search(f"q{turn}")
    assert len(provider._found_by) == plugin.provider.REMEMBERED_PAGES
    assert ("", "example.com/0/0") not in provider._found_by and ("", "example.com/119/4") in provider._found_by
    assert list(tmp_path.iterdir()) == []


def test_a_page_no_reader_read_gets_its_own_error_and_the_others_still_return(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A), _page(PAGE_C, text="   "), _page("https://example.com/not-asked")]}
    rows = plugin.GrounderWebSearchProvider().extract([PAGE_A, PAGE_B, PAGE_C])
    assert [row.get("error") is None for row in rows] == [True, False, False]
    assert rows[0]["content"] == "page text"
    assert rows[1] == {"url": PAGE_B, "title": "", "content": "", "raw_content": "",
                       "error": "Grounder could not read this page, or its site does not allow it."}


def test_extract_finds_a_page_under_the_address_grounder_returned_it_at(plugin, stub, hermes):
    hermes.token = "t"
    asked = ["http://www.example.com/a/?utm_source=x#top", "https://example.com/p?id=2", "https://example.com/p?id=1"]
    stub.search_body = {"results": [_page("https://example.com/a", text="A"), _page("https://example.com/p?id=1", text="one"),
                                    _page("https://example.com/p?id=2", text="two")]}
    rows = plugin.GrounderWebSearchProvider().extract(asked)
    assert [row["content"] for row in rows] == ["A", "two", "one"]
    assert [row["url"] for row in rows] == asked


def test_a_long_page_comes_back_whole_from_a_read_by_search_too(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A, text="w" * 90000)]}
    row = plugin.GrounderWebSearchProvider().extract([PAGE_A])[0]
    assert len(row["content"]) == 90000 and not row["content"].endswith("…")
    read = plugin.GrounderWebSearchProvider().search(f"{PAGE_A} the whole page")["data"]["web"][0]
    assert len(read["description"]) == 90000 and not read["description"].endswith("…")


def test_a_whole_request_failure_is_every_pages_error_never_an_exception_and_never_retried(plugin, stub, hermes):
    hermes.token = "t"
    provider = plugin.GrounderWebSearchProvider()
    stub.search_status = 429
    stub.search_body = {"error": {"code": "rate_limited", "message": "60 per minute"}}
    rows = provider.extract([PAGE_A, PAGE_B])
    assert [row["url"] for row in rows] == [PAGE_A, PAGE_B]
    assert {row["error"] for row in rows} == {"Grounder rate limit reached: 60 per minute"}
    assert len(_searches(stub)) == 1
    hermes.raises = httpx.ConnectTimeout("timed out")
    assert provider.extract([PAGE_A])[0]["error"] == "Could not reach Grounder: timed out"


def test_extract_goes_with_a_key_when_one_is_set(plugin, stub, hermes, keyed):
    stub.search_body = {"results": [_page(PAGE_A)]}
    assert plugin.GrounderWebSearchProvider().extract([PAGE_A])[0]["content"] == "page text"
    assert hermes.calls == [] and _searches(stub)[0]["headers"]["Authorization"] == f"Bearer {keyed}"


def test_extract_with_no_pages_sends_nothing_and_past_the_limit_sends_ten(plugin, stub, hermes):
    hermes.token = "t"
    provider = plugin.GrounderWebSearchProvider()
    assert provider.extract([]) == [] and stub.requests == []
    many = [f"https://example.com/{i}" for i in range(12)]
    stub.search_body = {"results": [_page(url) for url in many[:10]]}
    rows = provider.extract([*many, many[0]])
    sent = json.loads(_searches(stub)[0]["body"])
    assert sent["urls"] == many[:10] and sent["top_k"] == 10 and len(rows) == 13
    assert rows[10]["error"] and rows[12]["content"] == "page text"


def test_a_query_of_addresses_stays_whole_addresses_within_the_limit(plugin):
    pages = [f"https://example.com/{'x' * 900}/{i}" for i in range(3)]
    query = plugin.provider._addresses(pages)
    assert len(query) <= 2000 and query.split() == pages[:2]
    assert plugin.provider._addresses(["https://example.com/" + "y" * 3000]) == ("https://example.com/" + "y" * 3000)[:2000]


def test_extract_logs_the_page_count_and_never_an_address(plugin, stub, hermes, caplog):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A)]}
    with caplog.at_level("DEBUG"):
        plugin.GrounderWebSearchProvider().extract([PAGE_A, PAGE_B])
    assert any("1 of 2 pages read" in record.getMessage() for record in caplog.records)
    assert not [r for r in caplog.records if "example.com" in r.getMessage()]


def test_grounder_is_ready_whatever_is_set_so_hermes_keeps_the_web_tools(plugin, monkeypatch):
    # With no selected backend ready, Hermes offers the model neither web_search nor web_extract; a free search needs no key and no client.
    provider = plugin.GrounderWebSearchProvider()
    assert provider.is_available()
    monkeypatch.setenv("GROUNDER_OAUTH_CLIENT_ID", "https://api.grounder.net/oauth/clients/hermes-agent")
    assert provider.is_available()
    assert plugin.provider.CLIENT_ENV == manifest()["oauth"][0]["client_id_env"]


class Ctx:
    """The part of Hermes's plugin context ``register`` uses; ``state`` lasts across loads, as Hermes's does."""

    def __init__(self):
        self.providers = []
        self.state = self
        self.saved = {}

    def register_web_search_provider(self, provider):
        self.providers.append(provider)

    def register_hook(self, name, callback):
        pass

    def get(self, key, default=None):
        return self.saved.get(key, default)

    def set(self, key, value):
        self.saved[key] = value


def _web(home):
    return yaml.safe_load((home / "config.yaml").read_text())["web"]


def _env(home):
    path = home / ".env"
    return path.read_text() if path.exists() else ""


CLIENT_LINE = "GROUNDER_OAUTH_CLIENT_ID=https://api.grounder.net/oauth/clients/hermes-agent"


def test_the_first_load_selects_both_backends_and_keeps_the_web_tools_on(plugin, tmp_path):
    ctx = Ctx()
    plugin.register(ctx)
    assert [p.name for p in ctx.providers] == ["grounder"]
    assert _web(tmp_path) == {"search_backend": "grounder", "extract_backend": "grounder"}
    assert CLIENT_LINE in _env(tmp_path)
    assert ctx.providers[0].is_available()


def test_the_first_load_replaces_another_backend_and_keeps_the_rest_of_the_config(plugin, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(
        {"model": {"default": "m"}, "web": {"backend": "firecrawl", "search_backend": "tavily", "keyless_fallback": False}}))
    plugin.register(Ctx())
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved["model"] == {"default": "m"}
    assert saved["web"] == {"backend": "firecrawl", "search_backend": "grounder", "extract_backend": "grounder",
                            "keyless_fallback": False}


def test_a_later_choice_stands(plugin, tmp_path):
    ctx = Ctx()
    plugin.register(ctx)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"web": {"search_backend": "firecrawl", "extract_backend": "firecrawl"}}))
    plugin.register(ctx)
    assert _web(tmp_path) == {"search_backend": "firecrawl", "extract_backend": "firecrawl"}


def test_a_search_key_user_gets_no_sign_in_client(plugin, tmp_path, keyed):
    plugin.register(Ctx())
    assert _web(tmp_path) == {"search_backend": "grounder", "extract_backend": "grounder"}
    assert _env(tmp_path) == ""


def test_a_managed_install_is_left_alone(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "is_managed", lambda: True)
    ctx = Ctx()
    plugin.register(ctx)
    assert not (tmp_path / "config.yaml").exists() and _env(tmp_path) == "" and ctx.saved == {}
    assert [p.name for p in ctx.providers] == ["grounder"]


def test_a_failed_write_keeps_the_backend_and_tries_again_next_load(plugin, tmp_path, monkeypatch):
    real = plugin.save_config

    def refuse(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(plugin, "save_config", refuse)
    ctx = Ctx()
    plugin.register(ctx)
    assert [p.name for p in ctx.providers] == ["grounder"] and ctx.saved == {}
    monkeypatch.setattr(plugin, "save_config", real)
    plugin.register(ctx)
    assert _web(tmp_path) == {"search_backend": "grounder", "extract_backend": "grounder"}
    assert ctx.saved == {plugin.SELECTED_MARK: True}


ROUTED = {"query": "q", "results": [RESULT], "usage": {"latency_ms": 1432},
          "routing": {"calls": [{"provider": "exa", "status": "ok", "result_count": 4}, {"provider": "xai", "status": "ok", "result_count": 2},
                                {"provider": "brave", "status": "error", "result_count": 0}, {"provider": "tavily", "status": "ok", "result_count": 0}],
                      "live_calls": [{"provider": "espn", "status": "ok", "result_count": 1}]}}


def test_a_search_asks_for_citations_last_and_only_with_results(plugin, stub, keyed):
    stub.search_body = ROUTED
    result = plugin.GrounderWebSearchProvider().search("q")
    # The search itself carries no notes: after_tool adds them to the turn's first result Grounder served (GRO-698).
    assert list(result) == ["success", "data"]
    provider = plugin.GrounderWebSearchProvider()
    first = json.loads(provider.after_tool("web_search", {"query": "q"}, json.dumps(provider.search("q")), session_id="s", turn_id="t"))
    assert list(first)[-1:] == ["cite_sources"] and "pages" not in first  # the note that said Grounder saves no page to a file is gone: Hermes's own footer is true now
    assert first["cite_sources"].startswith("Cite every result your answer uses by its position in square brackets, like [3]. Positions belong to this answer alone")
    assert "write no Sources, References or Citations list" in first["cite_sources"]
    stub.search_body = {"query": "q", "results": []}
    empty = plugin.GrounderWebSearchProvider().search("q")
    assert "cite_sources" not in empty


def test_a_row_says_where_its_result_came_from_as_the_card_does(plugin, stub, keyed):
    stub.search_body = {"query": "q", "results": [{**RESULT, "published_date": "2026-11-05", "provenance": {
        "provider": "newsapi", "features": ["news"], "licensed": True, "licensed_basis": "robots-allowed",
        "publisher": {"name": "AP", "domain": "apnews.com"}}}, RESULT]}
    rows = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    # The provenance stays in Grounder's records and response; the model reads the date alone (GRO-698).
    assert rows[0] == {"title": "A", "url": RESULT["url"], "description": rows[0]["description"], "position": 1, "date": "2026-11-05"}
    assert "date" not in rows[1] and "source" not in rows[0] and "source" not in rows[1]


def test_an_answer_that_searched_ends_with_the_card_line_once(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    shown = json.dumps(provider.search("q"))
    # A result with no rows is one the model was shown nothing of: it is not a source and not a line.
    assert provider.after_tool("web_search", {"query": "q"}, "{}", session_id="s", turn_id="t0") is None
    assert provider.after_answer("The answer.\n", session_id="s", turn_id="t0") is None
    provider.after_tool("web_search", {"query": "q"}, shown, session_id="s", turn_id="t")
    assert provider.after_answer("The answer.\n", session_id="s", turn_id="t") == "The answer.\n\nSearched with Grounder optimized · via Exa, xAI +1 · 1 source · 1.4 s"
    assert provider.after_answer("Again.", session_id="s", turn_id="t") is None


def _two_searches(plugin, stub, monkeypatch, clock, answer="A"):
    """Two searches of one turn, each of 300 ms by Grounder's count, that end at the times ``clock`` gives; the line under the answer."""
    ticks = iter(clock)
    monkeypatch.setattr(plugin.provider, "_clock", lambda: next(ticks))
    provider = plugin.GrounderWebSearchProvider()
    for query in ("one", "two"):
        stub.search_body = {**ROUTED, "usage": {"latency_ms": 300}, "routing": {"calls": [{"provider": "exa", "status": "ok", "result_count": 1}]},
                            "results": [{**RESULT, "url": f"https://example.com/{query}"}]}
        provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query)), session_id="s", turn_id="t")
    return provider.after_answer(answer, session_id="s", turn_id="t")


def test_every_search_of_a_turn_is_on_the_one_line_and_the_time_is_what_the_user_waited(plugin, stub, keyed, monkeypatch):
    # One after the other, the waits add up.
    assert _two_searches(plugin, stub, monkeypatch, [10.0, 20.0]) == "A\n\nSearched with Grounder optimized · via Exa · 2 sources · 600 ms"


def test_searches_that_ran_together_share_their_time_and_the_line_counts_the_pages_once(plugin, stub, keyed, monkeypatch):
    # Two that overlap (parallel tool calls) took 400 ms of the user's time, not 600: 0.5.14 summed the latencies ("39 sources · 37.7 s" for five).
    assert _two_searches(plugin, stub, monkeypatch, [10.0, 10.1]) == "A\n\nSearched with Grounder optimized · via Exa · 2 sources · 400 ms"


def test_the_line_counts_the_rows_the_model_was_shown_not_the_rows_hermes_asked_for(plugin, stub, keyed):
    # Hermes asks the backend for a bucketed number of rows (3 becomes 10) and cuts them to the model's limit before the hook sees the result.
    stub.search_body = _routed(*(f"https://example.com/{i}" for i in range(10)))
    provider = plugin.GrounderWebSearchProvider()
    got = provider.search("q", limit=10)
    assert len(got["data"]["web"]) == 10
    cut = json.dumps({**got, "data": {"web": got["data"]["web"][:3]}})
    provider.after_tool("web_search", {"query": "q", "limit": 3}, cut, session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t").endswith("· 3 sources · 1.4 s")


def test_a_search_answered_again_from_hermess_cache_is_counted_but_not_timed_again(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    shown = json.dumps(provider.search("q"))
    provider.after_tool("web_search", {"query": "q"}, shown, session_id="s", turn_id="t1")
    assert provider.after_answer("A", session_id="s", turn_id="t1").endswith("· 1 source · 1.4 s")
    provider.after_tool("web_search", {"query": "q"}, shown, session_id="s", turn_id="t2")  # the plugin was not asked: Hermes had the answer
    assert provider.after_answer("A", session_id="s", turn_id="t2").endswith("· 1 source")


def test_no_line_for_a_turn_grounder_did_not_search(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    shown = json.dumps(provider.search("q"))
    provider.after_tool("web_search", {"query": "another backend answered this"}, shown, session_id="s", turn_id="t")
    provider.after_tool("terminal", {"command": "ls"}, "{}", session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t") is None
    provider.after_tool("web_search", {"query": "q"}, shown, session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="other", turn_id="t") is None


def test_a_search_without_results_adds_no_line(plugin, stub, keyed):
    stub.search_body = {"query": "q", "results": []}
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.after_tool("web_search", {"query": "q"}, "{}", session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t") is None


def test_a_read_grounder_served_asks_for_citations_outside_the_page(plugin, stub, keyed):
    url = "https://example.com/a"
    stub.search_body = {"query": "q", "results": [{**RESULT, "url": url, "text": "page text"}]}
    provider = plugin.GrounderWebSearchProvider()
    provider.extract([url])
    hermes_result = json.dumps({"results": [{"url": url, "title": "A", "content": "page text", "error": None}]})
    data = json.loads(provider.after_tool("web_extract", {"urls": [url]}, hermes_result))
    assert list(data) == ["results", "cite_sources"] and data["results"][0]["content"] == "page text"
    assert not hasattr(plugin.provider, "FILES_NOTE") and not hasattr(plugin.provider, "FILES_KEY")


def test_the_notes_come_once_a_turn_on_its_first_result_grounder_served(plugin, stub, keyed, in_chat):
    # GRO-698: every search of a turn repeated about 680 characters of notes; an answer needs them once.
    url = "https://example.com/a"
    stub.search_body = {"query": "q", "results": [{**RESULT, "url": url, "text": "page text"}]}
    provider = plugin.GrounderWebSearchProvider()
    searched = lambda query, turn: provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query)), session_id="s", turn_id=turn)
    assert json.loads(searched("one", "t1"))["cite_sources"] == plugin.provider.CITE_REQUEST
    # Later results of the turn carry their numbers and no notes (GRO-701).
    assert "cite_sources" not in json.loads(searched("two", "t1"))
    provider.extract([url])
    page = json.dumps({"results": [{"url": url, "title": "A", "content": "page text", "error": None}]})
    assert list(json.loads(provider.after_tool("web_extract", {"urls": [url]}, page, session_id="s", turn_id="t1"))) == ["results"]
    # The next turn, and a read that comes first in its turn, get them again.
    assert json.loads(provider.after_tool("web_extract", {"urls": [url]}, page, session_id="s", turn_id="t2"))["cite_sources"] == plugin.provider.CITE_REQUEST
    assert "cite_sources" not in json.loads(searched("three", "t2"))
    assert "cite_sources" in json.loads(searched("four", "t3"))
    # A tool's own search, and a host that names no turn, which gets them every time.
    tool = provider.after_tool(plugin.provider.SEARCH_TOOL, {"query": "five"}, provider.search_tool({"query": "five"}), session_id="s", turn_id="t4")
    assert list(json.loads(tool))[-1:] == ["cite_sources"]
    assert json.loads(searched("six", ""))["cite_sources"] == json.loads(searched("seven", ""))["cite_sources"] == plugin.provider.CITE_REQUEST_LISTED
    # A search that found nothing, or another backend's, gets none and does not use the turn's notes up.
    stub.search_body = {"query": "q", "results": []}
    assert searched("eight", "t5") is None
    assert provider.after_tool("web_search", {"query": "never searched by Grounder"}, json.dumps({"success": True, "data": {"web": [{"title": "x"}]}}),
                               session_id="s", turn_id="t5") is None
    stub.search_body = {"query": "q", "results": [{**RESULT, "url": url, "text": "page text"}]}
    assert "cite_sources" in json.loads(searched("nine", "t5"))



# --- citations by number (GRO-701) -----------------------------------------------------------------------------------

def _routed(*urls):
    return {**ROUTED, "results": [{**RESULT, "url": url, "title": f"Title {url[-1]}"} for url in urls]}


def test_results_are_numbered_across_the_turn_and_a_page_keeps_its_number(plugin, stub, keyed, in_chat):
    provider = plugin.GrounderWebSearchProvider()
    searched = lambda query, turn="t": json.loads(provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query)),
                                                                      session_id="s", turn_id=turn))
    stub.search_body = _routed(PAGE_A, PAGE_B)
    assert [row["position"] for row in searched("one")["data"]["web"]] == [1, 2]
    stub.search_body = _routed(PAGE_C, "https://www.example.com/a/")
    # A page the turn already numbered keeps its number, under any form of its address.
    assert [row["position"] for row in searched("two")["data"]["web"]] == [3, 1]
    stub.search_body = {"results": [_page(PAGE_B), _page("https://example.com/d")]}
    provider.extract([PAGE_B, "https://example.com/d"])
    pages = json.dumps({"results": [{"url": PAGE_B, "title": "T", "content": "x", "error": None},
                                    {"url": "https://example.com/d", "title": "T", "content": "x", "error": None},
                                    {"url": "https://example.com/e", "title": "", "content": "", "error": "could not read"}]})
    read = json.loads(provider.after_tool("web_extract", {}, pages, session_id="s", turn_id="t"))["results"]
    assert [page.get("position") for page in read] == [2, 4, None]
    # The next turn starts again at 1, and its answer cites only its own turn's results (GRO-835).
    stub.search_body = _routed(PAGE_C, "https://example.com/z")
    assert [row["position"] for row in searched("three", "t2")["data"]["web"]] == [1, 2]


def test_the_plugin_writes_the_sources_list_of_what_the_answer_cites_then_the_card_line(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A, PAGE_B, PAGE_C)
    provider.after_tool("web_search", {"query": "q"}, json.dumps(provider.search("q")), session_id="s", turn_id="t")
    answer = provider.after_answer("Wind is cheap [3], and cheaper still [1, 3].\n", session_id="s", turn_id="t")
    assert answer == ("Wind is cheap [3], and cheaper still [1, 3].\n\nSources:\n"
                      f"- Title a — {PAGE_A}\n- Title c — {PAGE_C}\n\n"
                      "Searched with Grounder optimized · via Exa, xAI +1 · 3 sources · 1.4 s")
    # A range counts each number in it, and a number no result has is left alone.
    provider.after_tool("web_search", {"query": "q"}, json.dumps(provider.search("q")), session_id="s", turn_id="t2")
    assert "- Title b" in provider.after_answer("See [2-3] and [9].", session_id="s", turn_id="t2")


def test_no_list_is_added_to_an_answer_that_wrote_its_own_or_cites_none(plugin, stub, keyed, in_chat):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A)
    card = "Searched with Grounder optimized · via Exa, xAI +1 · 1 source · 1.4 s"
    for turn, text in (("t1", "It is so [1].\n\n**Sources**\n1. A — https://example.com/a"), ("t2", "It is so [1].\n## References\n- a"),
                       ("t3", "It is so, as Example says.")):
        provider.after_tool("web_search", {"query": "q"}, json.dumps(provider.search("q")), session_id="s", turn_id=turn)
        assert provider.after_answer(text, session_id="s", turn_id=turn) == f"{text}\n\n{card}"
    # A turn that only read a page gets its list and no card line.
    stub.search_body = {"results": [_page(PAGE_A)]}
    provider.extract([PAGE_A])
    page = json.dumps({"results": [{"url": PAGE_A, "title": "T", "content": "x", "error": None}]})
    provider.after_tool("web_extract", {}, page, session_id="s", turn_id="t4")
    assert provider.after_answer("Read [1].", session_id="s", turn_id="t4") == f"Read [1].\n\nSources:\n- T — {PAGE_A}"


def test_a_host_that_names_no_turn_keeps_the_searchs_positions_and_the_model_writes_the_list(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A, PAGE_B)
    for query in ("one", "two"):
        data = json.loads(provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query))))
        assert [row["position"] for row in data["data"]["web"]] == [1, 2]
        assert data["cite_sources"] == plugin.provider.CITE_REQUEST_LISTED and "Sources list" in data["cite_sources"]


# --- another part of a page (GRO-691) --------------------------------------------------------------------------------

def test_a_search_that_names_a_page_reads_it_for_the_rest_of_the_query(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = {"results": [_page(PAGE_A, text="Books\n* 2020: 7 Ways\n* 2021: Together", title="Jamie Oliver")]}
    result = provider.search(f"{PAGE_A}, books section list")
    assert json.loads(_searches(stub)[-1]["body"]) == {"query": "books section list", "urls": [PAGE_A], "top_k": 1, "features": ["page-text"]}
    # The page's text as Grounder read it, its lines kept, in web_search's shape; served, so it is numbered and noted.
    assert result == {"success": True, "data": {"web": [{"title": "Jamie Oliver", "url": PAGE_A, "description": "Books\n* 2020: 7 Ways\n* 2021: Together",
                                                           "position": 1}]}}
    noted = json.loads(provider.after_tool("web_search", {"query": f"{PAGE_A}, books section list"}, json.dumps(result), session_id="s", turn_id="t"))
    assert noted["data"]["web"][0]["position"] == 1 and noted["cite_sources"] == plugin.provider.CITE_REQUEST
    assert provider.after_answer("Two [1].", session_id="s", turn_id="t").startswith("Two [1].\n\nSources:\n- Jamie Oliver — ")


def test_a_search_of_addresses_alone_reads_them_against_the_search_that_found_them(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = {"results": [{**RESULT, "url": PAGE_A}]}
    provider.search("offshore wind costs")
    stub.search_body = {"results": [_page(PAGE_A)]}
    provider.search(f"{PAGE_A}")
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "offshore wind costs"
    # The read names the page rather than finding it: a later extract still ranks against the search that found it.
    provider.search(f"{PAGE_A} turbines")
    provider.extract([PAGE_A])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "offshore wind costs"


def test_a_page_read_by_search_that_fails_says_so_and_sends_one_search(plugin, stub, keyed):
    stub.search_body = {"results": []}
    result = plugin.GrounderWebSearchProvider().search(f"what is on {PAGE_A} and {PAGE_B}?")
    assert result == {"success": False, "error": "Grounder could not read these pages, or its site does not allow it."}
    body = json.loads(_searches(stub)[-1]["body"])
    assert body["urls"] == [PAGE_A, PAGE_B] and body["query"] == "what is on and" and len(_searches(stub)) == 1


# --- a failed read keeps what the search showed (GRO-694) -----------------------------------------------------------

def test_a_failed_read_of_a_page_a_search_returned_gives_what_that_search_showed(plugin, stub, keyed, in_chat):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = {"results": [{**RESULT, "url": PAGE_A, "title": "Page A", "text": "the passage the search showed"}]}
    provider.search("q")
    stub.search_body = {"results": []}
    rows = provider.extract([PAGE_A, PAGE_B])
    note = plugin.provider.SHOWN_NOTE
    # No error on it, so Hermes keeps it as read and does not fetch the page again; a page no search returned keeps its error.
    assert rows[0] == {"url": PAGE_A, "title": "Page A", "content": f"{note}\n\nthe passage the search showed",
                       "raw_content": f"{note}\n\nthe passage the search showed"}
    assert rows[1]["error"] and not rows[1]["content"]
    # It is Grounder's, so it is numbered and cited like a read.
    page = json.dumps({"results": [{"url": PAGE_A, "title": "Page A", "content": rows[0]["content"], "error": None}]})
    assert json.loads(provider.after_tool("web_extract", {}, page, session_id="s", turn_id="t"))["results"][0]["position"] == 1
    # The same holds for a read by search.
    result = provider.search(f"{PAGE_A} more of it")
    assert result["success"] and result["data"]["web"][0]["description"].startswith(note)

def test_a_read_grounder_did_not_serve_is_left_alone(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    other = json.dumps({"results": [{"url": "https://example.com/other", "title": "", "content": "text", "error": None}]})
    assert provider.after_tool("web_extract", {}, other) is None
    assert provider.after_tool("web_extract", {}, "not json") is None
    assert provider.after_tool("web_extract", {}, json.dumps({"success": False, "error": "x"})) is None


def test_searches_of_one_turn_share_a_prompt_id_and_the_next_turn_gets_its_own(plugin, stub, keyed, monkeypatch):
    provider_module = sys.modules[plugin.GrounderWebSearchProvider.__module__]
    monkeypatch.setenv("HERMES_SESSION_ID", "session-1")
    provider = plugin.GrounderWebSearchProvider()
    sent = []
    for turn in ("turn-1", "turn-1", "turn-2"):
        provider_module.note_turn(session_id="session-1", conversation_id="chat-1", turn_id=turn, tool_name="web_search")
        provider.search("query")
        sent.append(json.loads(_searches(stub)[-1]["body"])["context"])
    assert sent[0] == sent[1] and sent[0]["prompt_ref"] != sent[2]["prompt_ref"]
    assert sent[0]["session_ref"] == sent[2]["session_ref"]
    assert all(re.fullmatch(r"[a-f0-9]{32}", value) for row in sent for value in row.values())
    assert not {"session-1", "chat-1", "turn-1"} & {value for row in sent for value in row.values()}


def test_a_search_names_the_model_whose_answer_asked_for_it(plugin, stub, keyed, monkeypatch):
    provider_module = sys.modules[plugin.GrounderWebSearchProvider.__module__]
    monkeypatch.setenv("HERMES_SESSION_ID", "session-2")
    provider = plugin.GrounderWebSearchProvider()
    provider.search("query")
    assert "agent" not in json.loads(_searches(stub)[-1]["body"])["context"]
    provider_module.note_model(session_id="session-2", model="anthropic/claude-sonnet-4.6", response_model="claude-sonnet-4-6-20260101", provider="anthropic")
    provider.search("query")
    assert json.loads(_searches(stub)[-1]["body"])["context"]["agent"] == {"model": "claude-sonnet-4-6-20260101"}
    provider_module.note_model(session_id="session-2", model="openrouter/auto", response_model=None)
    provider.search("query")
    assert json.loads(_searches(stub)[-1]["body"])["context"]["agent"] == {"model": "openrouter/auto"}
    provider_module.note_model(session_id="other", model="x/y")
    assert json.loads(_searches(stub)[-1]["body"])["context"]["agent"] == {"model": "openrouter/auto"}


def test_a_search_outside_a_hermes_session_sends_no_context(plugin, stub, keyed, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    plugin.GrounderWebSearchProvider().search("query")
    assert "context" not in json.loads(_searches(stub)[-1]["body"])


def test_grounder_search_sends_every_option_as_the_mcp_search_maps_it(plugin, stub, keyed):
    stub.search_body = ROUTED
    out = json.loads(plugin.GrounderWebSearchProvider().search_tool({
        "query": " q ", "top_k": 3, "days": 7, "domains": ["bbc.com"], "features": ["news"], "include_text": False, "requested_provider": "exa",
        "providers": ["exa"], "routing_settings": {"fallback": False}, "workspace": "work", "user_location": " Austin ", "user_time_zone": "America/Chicago"}))
    sent = json.loads(_searches(stub)[0]["body"])
    # The model named a provider, so the search is Grounder direct.
    assert sent == {"query": "q", "mode": "direct", "top_k": 3, "include_text": False, "requested_provider": "exa", "providers": ["exa"], "features": ["news"],
                    "routing_settings": {"fallback": False}, "workspace": "work",
                    "filters": {"domains": {"values": ["bbc.com"], "mode": "include"}, "recency": {"mode": "rolling", "days": 7}},
                    "context": {"user_location": "Austin", "user_time_zone": "America/Chicago"}}
    assert out["success"] and out["data"]["web"][0]["title"] == "A" and "cite_sources" not in out


def test_grounder_search_reads_pages_whole_and_reports_failures(plugin, stub, keyed):
    stub.search_body = {"query": "q", "results": [{**RESULT, "text": "x" * 50000}]}
    out = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q", "urls": ["https://example.com/a"]}))
    assert "text_max_chars" not in json.loads(_searches(stub)[0]["body"])
    assert len(out["data"]["web"][0]["description"]) == 50000
    assert not json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": " "}))["success"]
    stub.search_status = 500
    assert not json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q"}))["success"]


def test_grounder_search_gets_the_card_line(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    shown = provider.search_tool({"query": "q"})
    provider.after_tool("grounder_search", {"query": "q"}, shown, session_id="s", turn_id="t")
    assert "Searched with Grounder" in provider.after_answer("A", session_id="s", turn_id="t")


def test_list_providers_says_what_the_mcp_tool_says_and_sends_no_credential(plugin, stub, keyed, monkeypatch):
    monkeypatch.setattr(plugin.provider, "FEATURES_URL", f"{stub.origin}/v1/features")
    stub.features_body = {"features": [{"id": "news", "description": "d", "returns": "r", "inputs": ["query"], "specialized": ["espn"],
                                        "offerings": [{"provider": "exa", "mode": None}, {"provider": "brave", "mode": "news"}]}]}
    out = json.loads(plugin.GrounderWebSearchProvider().list_providers_tool({}))
    assert out["data"]["features"] == [{"feature": "news", "description": "d", "returns": "r", "inputs": ["query"], "providers": {"exa": [], "brave": ["news"]}, "apis": ["espn"]}]
    assert "search_modes" not in out["data"]  # the stub lists none
    assert all("Authorization" not in request["headers"] for request in stub.requests)


def test_list_providers_also_lists_the_search_modes_as_grounder_lists_them_now(plugin, stub, keyed):
    stub.profiles_body = {"profiles": [{"id": "fast", "name": "Fast", "description": "Quick"}, {"id": "x", "description": "No name"}, {"name": "no id"}]}
    out = json.loads(plugin.GrounderWebSearchProvider().list_providers_tool({}))
    assert out["data"]["search_modes"] == [{"id": "fast", "name": "Fast", "description": "Quick"}, {"id": "x", "name": "x", "description": "No name"}]
    stub.profiles_body = {"profiles": [{"id": "later", "name": "Later", "description": ""}]}
    assert [m["id"] for m in json.loads(plugin.GrounderWebSearchProvider().list_providers_tool({}))["data"]["search_modes"]] == ["later"]


def test_a_hermes_that_refuses_the_tools_keeps_web_search(plugin):
    calls = []

    class Refuses:
        state = {}

        def register_web_search_provider(self, provider):
            calls.append(provider)

        def register_hook(self, name, callback):
            pass

        def register_tool(self, **kwargs):
            raise ValueError("no")

    plugin.register(Refuses())
    assert calls[0].name == "grounder"


# --- why a page was not read (GRO-687) -------------------------------------------------------------------------------

def test_a_failed_read_says_why_when_the_api_knows(plugin, stub, keyed):
    stub.search_body = {"results": [], "unread": [{"url": PAGE_A, "reason": "not_found"}, {"url": "https://www.example.com/b/", "reason": "timed_out"},
                                                   {"url": PAGE_C, "reason": "unreadable"}]}
    rows = plugin.GrounderWebSearchProvider().extract([PAGE_A, PAGE_B, PAGE_C, "https://example.com/d"])
    assert [row["error"] for row in rows] == [plugin.provider.UNREAD["not_found"], plugin.provider.UNREAD["timed_out"], plugin.provider.NOT_READ,
                                              plugin.provider.NOT_READ]
    assert "404" in rows[0]["error"] and "right address" in rows[0]["error"]
    # A read by search says it too.
    stub.search_body = {"results": [], "unread": [{"url": PAGE_A, "reason": "refused"}]}
    assert plugin.GrounderWebSearchProvider().search(f"{PAGE_A} books") == {"success": False, "error": plugin.provider.UNREAD["refused"]}


# --- Grounder's three modes: the request's `mode`, and the card line --------------------------------------------------------

def _choose(tmp_path, **grounder):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"web": {"grounder": grounder}}))


def _last(stub):
    return json.loads(_searches(stub)[-1]["body"])


def _signed_or_keyed(request, how):
    if how == "sign-in":
        request.getfixturevalue("hermes").token = "t"
    else:
        request.getfixturevalue("keyed")


@pytest.mark.parametrize("how", ["key", "sign-in"])
@pytest.mark.parametrize("saved,passed,mode,profile", [
    ({}, {}, "optimized", None),  # nothing chosen: Grounder picks the provider
    ({"live_data_off": ["espn"]}, {}, "optimized", None),  # a live-data API off changes no mode
    ({"server": "api:espn"}, {}, "optimized", None),  # a pinned live-data API is no provider pin
    ({"preset": "fast"}, {}, "preset", "fast"),
    ({"server": "provider:exa"}, {}, "direct", None),
    ({"server": "provider:exa", "server_mode": "deep"}, {}, "direct", None),
    ({}, {"profile": "fast"}, "preset", "fast"),  # a model-passed profile is a preset
    ({}, {"profile": "auto"}, "optimized", "auto"),  # auto is none
    ({"preset": "deep"}, {"profile": "fast"}, "preset", "fast"),  # the model's preset stands over the user's preset, as it always did
    ({"server": "provider:exa"}, {"profile": "auto"}, "direct", None),
    ({}, {"requested_provider": "exa"}, "direct", None),  # a provider the model names is a direct search (Grounder 0.9.0 reads it so)
    ({"preset": "fast"}, {"requested_provider": "exa"}, "direct", None),  # and it has no preset
    ({}, {"providers": ["exa"]}, "direct", None),
    ({"preset": "fast"}, {"providers": ["exa"], "profile": "deep"}, "direct", None),
    ({"preset": "fast"}, {"requested_provider": "  "}, "preset", "fast"),  # a blank name names none
])
def test_a_search_with_the_users_choices_names_its_mode_from_what_the_merged_request_is(plugin, stub, request, tmp_path, how, saved, passed, mode, profile):
    _signed_or_keyed(request, how)
    _choose(tmp_path, **saved)
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", **passed}) if passed else provider.search("q")
    sent = _last(stub)
    assert sent["mode"] == mode and sent.get("profile") == profile
    if sent.get("routing_settings", {}).get("exclusive") is True:
        assert mode == "direct"


@pytest.mark.parametrize("how", ["key", "sign-in"])
def test_a_direct_search_has_no_profile_whoever_chose_the_preset_and_a_preset_alone_is_a_preset(plugin, stub, request, tmp_path, how):
    _signed_or_keyed(request, how)
    provider = plugin.GrounderWebSearchProvider()
    # A saved direct pin and a model-passed profile: direct, and no profile is sent, which Grounder 0.9.0 would refuse beside it.
    _choose(tmp_path, server="provider:exa")
    provider.search_tool({"query": "q", "profile": "fast"})
    sent = _last(stub)
    assert sent["mode"] == "direct" and "profile" not in sent and sent["routing_settings"]["exclusive"] is True
    # Only a saved preset: preset.
    _choose(tmp_path, preset="fast")
    provider.search("q")
    sent = _last(stub)
    assert sent["mode"] == "preset" and sent["profile"] == "fast" and "exclusive" not in json.dumps(sent)
    # A model-passed exclusive pin and a saved preset: direct, and no profile.
    provider.search_tool({"query": "q", "routing_settings": {"providers": {"exa": {"use": "always"}}, "exclusive": True}})
    sent = _last(stub)
    assert sent["mode"] == "direct" and "profile" not in sent and sent["routing_settings"]["exclusive"] is True
    # A model that names the provider on grounder_search: direct, and no profile, over a saved preset.
    _choose(tmp_path, preset="fast")
    provider.search_tool({"query": "q", "requested_provider": "exa"})
    sent = _last(stub)
    assert sent["mode"] == "direct" and "profile" not in sent and sent["requested_provider"] == "exa"
    # Both a saved preset and a saved pin, by hand: direct.
    _choose(tmp_path, preset="fast", server="provider:exa")
    provider.search("q")
    assert _last(stub)["mode"] == "direct" and "profile" not in _last(stub)


def test_a_free_search_sends_no_mode_and_no_profile(plugin, stub, free, tmp_path):
    _choose(tmp_path, preset="deep", server="provider:exa")
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search_tool({"query": "q", "profile": "fast"})
    assert len(_searches(stub)) == 2
    assert all("mode" not in body and "profile" not in body for body in _bodies(stub))


@pytest.mark.parametrize("how", ["key", "sign-in"])
def test_a_page_read_sends_no_mode(plugin, stub, request, tmp_path, how):
    _signed_or_keyed(request, how)
    _choose(tmp_path, preset="deep")
    stub.search_body = {"results": [_page(PAGE_A)]}
    provider = plugin.GrounderWebSearchProvider()
    provider.extract([PAGE_A])
    provider.search(f"{PAGE_A} books")
    provider.search_tool({"query": "q", "urls": [PAGE_A], "profile": "fast"})
    bodies = _bodies(stub)
    assert len(bodies) == 3 and all(body["urls"] == [PAGE_A] for body in bodies)
    assert all("mode" not in body for body in bodies)


def test_a_mode_the_caller_already_set_is_kept(plugin, stub, keyed):
    plugin.provider._send({"query": "q", "mode": "preset", "profile": "fast"}, {"routing_settings": {"exclusive": True}})
    assert _last(stub)["mode"] == "preset" and _last(stub)["profile"] == "fast"
    plugin.provider._send({"query": "q"}, {})
    assert _last(stub)["mode"] == "optimized"
    plugin.provider._send({"query": "q"}, None)  # no extra: a page read, which names no mode
    assert "mode" not in _last(stub)


def test_whether_a_direct_search_lets_grounder_pick_when_its_provider_finds_nothing_is_one_switch_and_is_off(plugin, stub, keyed, tmp_path, monkeypatch):
    assert plugin.provider.DIRECT_FALLBACK is False  # Harlan, 2026-11-12: "user does for direct": a direct search returns what its provider gave, even nothing
    _choose(tmp_path, server="provider:exa")
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    assert _last(stub)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False} and _last(stub)["mode"] == "direct"
    monkeypatch.setattr(plugin.provider, "DIRECT_FALLBACK", True)
    provider.search("q")
    assert _last(stub)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": True}


def _unseen(body):
    """A search body with no latency, so the line under an answer carries no time (the time of two searches that ran a moment apart is not what these tests are about)."""
    return {**body, "usage": {}}


@pytest.fixture
def lines(plugin, stub):
    """``lines(searches)``: for each ``(query, response body)`` run a ``web_search`` of one turn against the stub, and return the line under the answer."""
    provider = plugin.GrounderWebSearchProvider()

    def run(searches, turn="t"):
        for query, body in searches:
            stub.search_body = body
            provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query)), session_id="s", turn_id=turn)
        return (provider.after_answer("A", session_id="s", turn_id=turn) or "").removeprefix("A\n\n")

    return run


def test_the_card_line_names_the_mode_the_search_ran_in(plugin, stub, keyed, tmp_path, lines):
    assert lines([("one", ROUTED)]) == "Searched with Grounder optimized · via Exa, xAI +1 · 1 source · 1.4 s"
    _choose(tmp_path, preset="fast")
    assert lines([("two", ROUTED)], "t2") == "Searched with Grounder preset (Fast) · via Exa, xAI +1 · 1 source · 1.4 s"
    _choose(tmp_path, server="provider:exa")
    assert lines([("three", ROUTED)], "t3") == "Searched with Grounder direct · via Exa, xAI +1 · 1 source · 1.4 s"


def test_a_preset_is_named_by_its_id_capitalised_as_the_model_or_the_user_chose_it(plugin, stub, keyed, tmp_path):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = ROUTED
    shown = provider.search_tool({"query": "q", "profile": "balanced"})
    provider.after_tool("grounder_search", {"query": "q"}, shown, session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t").startswith("A\n\nSearched with Grounder preset (Balanced) · via Exa")
    provider.after_tool("grounder_search", {"query": "q"}, provider.search_tool({"query": "q", "profile": "auto"}), session_id="s", turn_id="t2")
    assert provider.after_answer("A", session_id="s", turn_id="t2").startswith("A\n\nSearched with Grounder optimized · via Exa")


def test_the_response_names_the_mode_when_it_says_one_of_the_three_and_the_request_when_it_does_not(plugin, stub, keyed, lines):
    # Grounder's own word wins over what was sent, and only the three names count; a preset's name comes from the request's profile when it sent one.
    assert lines([("a", {**ROUTED, "mode": "direct"})], "t1").startswith("Searched with Grounder direct · via Exa")
    assert lines([("b", {**ROUTED, "routing": {**ROUTED["routing"], "mode": "preset"}})], "t2").startswith("Searched with Grounder preset · via Exa")
    assert lines([("c", {**ROUTED, "mode": "direct", "routing": {**ROUTED["routing"], "mode": "preset"}})], "t3").startswith("Searched with Grounder preset · via Exa")  # routing.mode first
    for odd in ("turbo", "", None, 7, ["direct"], {"mode": "direct"}):
        assert lines([("d", {**ROUTED, "mode": odd})], "t4").startswith("Searched with Grounder optimized · via Exa"), odd  # the request said optimized


def _answered(**routing):
    """A response whose ``routing`` is as Grounder 0.9.0 gives it, with no latency and one result."""
    return {"query": "q", "results": [RESULT], "usage": {}, "routing": routing}


def test_the_card_line_reads_the_routing_of_a_grounder_that_names_its_mode_provider_and_preset(plugin, stub, keyed, tmp_path, lines):
    # routing.mode (optimized | preset | direct), routing.provider (the provider that answered), routing.provider_mode, routing.profile (a preset search's id) and
    # routing.fallback ({from, from_mode, reason}).
    assert lines([("a", _answered(mode="optimized", provider="exa", provider_mode=None))], "t1") == "Searched with Grounder optimized · via Exa · 1 source"
    assert lines([("b", _answered(mode="preset", profile="fast", provider="brave", provider_mode="news"))], "t2") == "Searched with Grounder preset (Fast) · via Brave · 1 source"
    # The preset that ran is the response's, not the one that was sent.
    _choose(tmp_path, preset="deep")
    assert lines([("c", _answered(mode="preset", profile="fast", provider="exa"))], "t3") == "Searched with Grounder preset (Fast) · via Exa · 1 source"
    assert lines([("c2", _answered(mode="preset", provider="exa"))], "t3b") == "Searched with Grounder preset (Deep) · via Exa · 1 source"  # none named: the one sent
    # A direct search whose provider found nothing: the provider that answered, not the one chosen.
    _choose(tmp_path, server="provider:exa")
    fallback = {"from": "exa", "from_mode": "deep", "reason": "no_results"}
    assert lines([("d", _answered(mode="direct", provider="brave", provider_mode=None, fallback=fallback))], "t4") == "Searched with Grounder direct · via Brave · 1 source"
    # routing.provider stands over the providers whose calls returned results.
    both = {**ROUTED, "usage": {}, "routing": {**ROUTED["routing"], "mode": "optimized", "provider": "tavily"}}
    assert lines([("e", both)], "t5") == "Searched with Grounder optimized · via Tavily · 1 source"
    assert lines([("f", {**both, "routing": {k: v for k, v in both["routing"].items() if k != "provider"}})], "t6") == "Searched with Grounder optimized · via Exa, xAI +1 · 1 source"


@pytest.mark.parametrize("routing,line", [
    (None, "Searched with Grounder optimized · 1 source"),
    ("direct", "Searched with Grounder optimized · 1 source"),
    ([], "Searched with Grounder optimized · 1 source"),
    ({}, "Searched with Grounder optimized · 1 source"),
    ({"provider": 7}, "Searched with Grounder optimized · 1 source"),
    ({"provider": "  ", "profile": 3, "mode": ["direct"]}, "Searched with Grounder optimized · 1 source"),
    ({"mode": "turbo", "profile": "auto", "provider_mode": 5, "fallback": "no"}, "Searched with Grounder optimized · 1 source"),
])
def test_a_response_whose_routing_fields_are_absent_or_of_another_shape_is_read_as_a_grounder_before_0_9_0(plugin, stub, keyed, lines, routing, line):
    assert lines([("a", {"query": "q", "results": [RESULT], "usage": {}, "routing": routing})]) == line
    # The same fields beside the calls the older response had: the calls say who answered.
    odd = routing if isinstance(routing, dict) else {}
    body = {**ROUTED, "usage": {}, "routing": {**ROUTED["routing"], **odd}}
    assert lines([("b", body)], "t2") == "Searched with Grounder optimized · via Exa, xAI +1 · 1 source"


def test_a_preset_search_with_no_name_anywhere_is_just_a_preset(plugin, stub, keyed, lines):
    assert lines([("a", _answered(mode="preset", profile="auto"))]) == "Searched with Grounder preset · 1 source"


def test_a_free_search_names_a_mode_only_when_the_response_does(plugin, stub, free, lines):
    assert lines([("a", ROUTED)], "t1") == "Searched with Grounder · via Exa, xAI +1 · 1 source · 1.4 s"
    assert lines([("b", {**ROUTED, "mode": "optimized"})], "t2") == "Searched with Grounder optimized · via Exa, xAI +1 · 1 source · 1.4 s"
    assert lines([("c", _answered(mode="direct", provider="brave"))], "t3") == "Searched with Grounder direct · via Brave · 1 source"


def test_a_page_read_by_search_names_a_mode_only_when_the_response_does(plugin, stub, keyed, tmp_path, lines):
    _choose(tmp_path, preset="fast")  # the read sends none of this
    page = {"results": [_page(PAGE_A)], "usage": {}}
    assert lines([(f"{PAGE_A} books", page)], "t1") == "Searched with Grounder · 1 source"
    assert lines([(f"{PAGE_A} books", {**page, "mode": "optimized"})], "t2") == "Searched with Grounder optimized · 1 source"


def test_searches_of_one_answer_in_different_modes_name_each_mode_once(plugin, stub, keyed, tmp_path, lines):
    provider = plugin.GrounderWebSearchProvider()
    for query, saved in (("one", {}), ("two", {"preset": "fast"}), ("three", {}), ("four", {"preset": "fast"}), ("five", {"preset": "deep"}), ("six", {"server": "provider:exa"})):
        _choose(tmp_path, **saved)
        stub.search_body = _unseen(_routed(f"https://example.com/{query}"))
        provider.after_tool("web_search", {"query": query}, json.dumps(provider.search(query)), session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t") == (
        "A\n\nSearched with Grounder optimized, Grounder preset (Fast), Grounder preset (Deep), Grounder direct · via Exa, xAI +1 · 6 sources")
    # Every search in one mode: that mode once.
    _choose(tmp_path, server="provider:exa")
    assert lines([("seven", _unseen(_routed("https://example.com/7"))), ("eight", _unseen(_routed("https://example.com/8")))], "t2").startswith(
        "Searched with Grounder direct · via Exa")
    # A page read that names no mode, among searches that have one, adds no name.
    _choose(tmp_path)
    stub.search_body = _unseen(_routed("https://example.com/9"))
    provider.after_tool("web_search", {"query": "nine"}, json.dumps(provider.search("nine")), session_id="s", turn_id="t3")
    stub.search_body = {"results": [_page(PAGE_A)], "usage": {}}
    provider.after_tool("web_search", {"query": f"{PAGE_A} ten"}, json.dumps(provider.search(f"{PAGE_A} ten")), session_id="s", turn_id="t3")
    assert provider.after_answer("A", session_id="s", turn_id="t3") == "A\n\nSearched with Grounder optimized · via Exa, xAI +1 · 2 sources"
