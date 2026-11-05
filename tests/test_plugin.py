# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from conftest import PLUGIN_DIR, manifest

RESULT = {"url": "https://example.com/a", "title": "A", "text": "first  passage\n text",
          "rank": 1, "provenance": {"found_by": ["brave"]}}
SIGN_IN = "Not signed in to Grounder. Run `hermes auth add grounder:grounder`."


def _searches(stub):
    return [r for r in stub.requests if r["path"] == "/v1/search"]


def test_without_sign_in_search_asks_for_hermes_sign_in_and_sends_nothing(plugin, stub, hermes):
    provider = plugin.GrounderWebSearchProvider()
    assert provider.name == "grounder" and provider.display_name == "Grounder"
    assert provider.supports_search() and not provider.supports_extract()
    assert not provider.is_available()
    assert provider.search("q") == {"success": False, "error": SIGN_IN}
    assert stub.requests == []


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
    assert call["json"] == {"query": "offshore wind", "top_k": 20, "text_max_chars": 800}
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
        assert "/oauth/" not in source.read_text(), source.name


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


def test_asks_grounder_for_the_passages_of_each_result_within_800_characters(plugin, stub, hermes):
    hermes.token = "t"
    plugin.GrounderWebSearchProvider().search("q")
    assert json.loads(_searches(stub)[0]["body"])["text_max_chars"] == plugin.provider.DESCRIPTION_CHARS == 800


def test_rows_carry_the_text_grounder_chose_as_it_came_on_one_line(plugin, stub, hermes):
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
        assert len(row["description"]) <= plugin.provider.DESCRIPTION_CHARS
        assert not row["description"].endswith("…") or sent.endswith("…"), name
    by_name = {row["title"]: row["description"] for row in rows}
    # The answer is in the row whether it lay at the head or far into the text; the first 800 characters of the late text did not hold it.
    assert "The capital of Freedonia is Fredville" in by_name["early"]
    assert "The capital of Freedonia is Fredville" in by_name["late"]
    assert "The capital of Freedonia is Fredville" in by_name["long"]
    assert shapes["late"]["whole_chars"] > 800 and shapes["long"]["whole_chars"] == 100000
    assert " … " in by_name["late"]
    # No match: Grounder cut the text at the budget; a text shorter than the budget stays whole.
    assert len(by_name["none"]) == 800 and "Fredville" not in by_name["none"]
    assert by_name["short"] == "Fredville is the capital of Freedonia."


def test_text_of_exactly_the_budget_is_not_cut(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [{**RESULT, "text": "word " * 159 + "words"}]}
    description = plugin.GrounderWebSearchProvider().search("q")["data"]["web"][0]["description"]
    assert len(description) == 800 and not description.endswith("…")


def test_a_response_that_ignored_the_budget_is_capped_at_800_on_one_line(plugin, stub, hermes):
    # A Grounder that does not know text_max_chars sends the whole text, and a summary has no budget: a row still never exceeds 800.
    hermes.token = "t"
    stub.search_body = {"results": [{**RESULT, "text": "word\n\t" * 400},
                                    {**RESULT, "url": "https://example.com/s", "summary": "s" * 2000, "text": "t"}]}
    rows = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    for row in rows:
        assert len(row["description"]) == plugin.provider.DESCRIPTION_CHARS == 800
        assert row["description"].endswith("…")
        assert "\n" not in row["description"] and "\t" not in row["description"]


def test_query_is_cut_to_2000_characters_and_the_limit_kept_in_range(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [RESULT, {**RESULT, "url": "https://example.com/b"}]}
    provider = plugin.GrounderWebSearchProvider()
    rows = provider.search("é" * 2500, limit=0)["data"]["web"]
    assert len(rows) == 1
    assert json.loads(_searches(stub)[0]["body"]) == {"query": "é" * 2000, "top_k": 1, "text_max_chars": 800}
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
    assert json.loads(request["body"]) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_a_refused_key_and_a_refused_sign_in_each_say_what_to_fix(plugin, stub, hermes, monkeypatch):
    hermes.token = "revoked-token"
    assert plugin.GrounderWebSearchProvider().search("q") == {
        "success": False, "error": "Grounder rejected the sign-in. Run `hermes auth add grounder:grounder`."}
    monkeypatch.setenv("GROUNDER_API_KEY", "revoked-key")
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", f"{stub.origin}/v1/search")
    assert plugin.GrounderWebSearchProvider().search("q") == {
        "success": False, "error": "Grounder rejected the search key in GROUNDER_API_KEY."}
    assert len(_searches(stub)) == 2 and len(hermes.calls) == 1


def test_what_hermes_raises_becomes_a_failure_not_an_exception(plugin, stub, hermes):
    provider = plugin.GrounderWebSearchProvider()
    hermes.raises = plugin.provider.AuthError(
        "grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder",
        code="oauth_client_id_missing")
    assert provider.search("q") == {"success": False, "error":
                                    "Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set."}
    hermes.raises = plugin.provider.ProviderNotSignedIn("grant revoked")
    assert provider.search("q") == {"success": False, "error": SIGN_IN}
    hermes.raises = PermissionError("Refusing to send the grounder sign-in to elsewhere.example")
    assert provider.search("q") == {"success": False, "error":
                                    "Hermes refused the Grounder sign-in: Refusing to send the grounder sign-in to elsewhere.example"}
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

        def register_cli_command(self, **kwargs):
            calls["command"] = kwargs

    plugin.register(Context())
    assert calls["provider"].name == "grounder"
    assert "command" not in calls


def test_setup_entry_asks_for_no_key_and_names_both_ways_in(plugin):
    entry = plugin.GrounderWebSearchProvider().get_setup_schema()
    assert entry["name"] == "Grounder" and entry["badge"] == "account"
    assert entry["env_vars"] == []
    assert "`hermes auth add grounder:grounder`" in entry["tag"] and "GROUNDER_API_KEY" in entry["tag"]
    assert "Search only" in entry["tag"]
