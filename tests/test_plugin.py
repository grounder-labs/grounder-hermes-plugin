# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import re
import stat
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RESULT = {"url": "https://example.com/a", "title": "A", "text": "first  passage\n text",
          "rank": 1, "provenance": {"found_by": ["brave"]}}
SECRET_TOKEN = "tok-never-printed"


def _save(plugin, access="current", expires_in=3600):
    plugin.auth.save_tokens({
        "access_token": access, "refresh_token": "refresh-0",
        "expires_at": int(time.time()) + expires_in, "scope": "search",
        "client_id": "client-1", "resource": plugin.auth.resource()})


def _searches(stub):
    return [r for r in stub.requests if r["path"] == "/v1/search"]


def _token_calls(stub):
    return [r for r in stub.requests if r["path"] == "/oauth/token"]


def _visit(url):
    # The browser following the redirect to the loopback callback; a refused callback answers 400.
    try:
        urllib.request.urlopen(url).read()
    except urllib.error.HTTPError:
        pass


def test_without_sign_in_search_asks_for_login_and_sends_nothing(plugin, stub):
    provider = plugin.GrounderWebSearchProvider()
    assert provider.name == "grounder" and provider.display_name == "Grounder"
    assert provider.supports_search() and not provider.supports_extract()
    assert not provider.is_available()
    assert provider.search("q") == {
        "success": False, "error": "Not signed in to Grounder. Run `hermes grounder login`."}
    assert stub.requests == []


def test_results_become_hermes_rows_in_grounder_order(plugin, stub):
    _save(plugin)
    stub.search_body = {"query": "q", "results": [
        RESULT, {**RESULT, "url": "https://example.com/b", "title": "B",
                 "summary": "short", "text": "x" * 5000}]}
    provider = plugin.GrounderWebSearchProvider()
    assert provider.is_available()
    result = provider.search("offshore wind", limit=50)
    assert result == {"success": True, "data": {"web": [
        {"title": "A", "url": "https://example.com/a",
         "description": "first passage text", "position": 1},
        {"title": "B", "url": "https://example.com/b",
         "description": "short", "position": 2}]}}
    request = _searches(stub)[0]
    assert json.loads(request["body"]) == {"query": "offshore wind", "top_k": 20, "text_max_chars": 800}
    assert request["headers"]["Authorization"] == "Bearer current"


def test_logs_the_result_count_and_never_the_query(plugin, stub, caplog):
    # A query is the user's words; a log line outlives the search. Every level, the failing paths too.
    _save(plugin)
    stub.search_body = {"results": [RESULT]}
    secret = "my unreleased merger with acme corp"
    with caplog.at_level("DEBUG"):
        assert plugin.GrounderWebSearchProvider().search(secret)["success"]
        stub.search_status = 500
        stub.search_body = {"error": {"code": "internal_error", "message": "down"}}
        assert not plugin.GrounderWebSearchProvider().search(secret)["success"]
    assert any("1 results" in record.getMessage() for record in caplog.records)
    assert not [r.getMessage() for r in caplog.records if "merger" in r.getMessage() or "acme" in r.getMessage()]
    assert not [r for r in caplog.records if secret in str(r.args) or secret in r.getMessage()]


def _api_shapes():
    # What POST /v1/search answers for this query at text_max_chars 800, recorded from the passage code the API runs
    # (workers/router/src/lib/passages.ts): the answer at the head, deep in the text, nowhere, a text shorter than the budget, and one of 100,000 characters.
    return json.loads((Path(__file__).parent / "api_shapes.json").read_text())


def test_asks_grounder_for_the_passages_of_each_result_within_800_characters(plugin, stub):
    _save(plugin)
    plugin.GrounderWebSearchProvider().search("q")
    assert json.loads(_searches(stub)[0]["body"])["text_max_chars"] == plugin.provider.DESCRIPTION_CHARS == 800


def test_rows_carry_the_text_grounder_chose_as_it_came_on_one_line(plugin, stub):
    # The answer early, the answer late, no match, very short, very long: every row is what the API sent (white space aside), none cut or re-chosen here.
    _save(plugin)
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


def test_text_of_exactly_the_budget_is_not_cut(plugin, stub):
    _save(plugin)
    stub.search_body = {"results": [{**RESULT, "text": "word " * 159 + "words"}]}
    description = plugin.GrounderWebSearchProvider().search("q")["data"]["web"][0]["description"]
    assert len(description) == 800 and not description.endswith("…")


def test_a_response_that_ignored_the_budget_is_capped_at_800_on_one_line(plugin, stub):
    # A Grounder that does not know text_max_chars sends the whole text, and a summary has no budget: a row still never exceeds 800.
    _save(plugin)
    stub.search_body = {"results": [{**RESULT, "text": "word\n\t" * 400},
                                    {**RESULT, "url": "https://example.com/s", "summary": "s" * 2000, "text": "t"}]}
    rows = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    for row in rows:
        assert len(row["description"]) == plugin.provider.DESCRIPTION_CHARS == 800
        assert row["description"].endswith("…")
        assert "\n" not in row["description"] and "\t" not in row["description"]


def test_query_is_cut_to_2000_characters_and_the_limit_kept_in_range(plugin, stub):
    _save(plugin)
    stub.search_body = {"results": [RESULT, {**RESULT, "url": "https://example.com/b"}]}
    provider = plugin.GrounderWebSearchProvider()
    rows = provider.search("é" * 2500, limit=0)["data"]["web"]
    assert len(rows) == 1
    assert json.loads(_searches(stub)[0]["body"]) == {"query": "é" * 2000, "top_k": 1, "text_max_chars": 800}
    provider.search("q")
    assert json.loads(_searches(stub)[1]["body"])["top_k"] == 5


def test_user_agent_names_the_plugin_at_its_manifest_version(plugin, stub):
    # Grounder names these searches Hermes Agent by this user agent; the version follows plugin.yaml.
    manifest = (Path(plugin.__file__).parent / "plugin.yaml").read_text()
    version = re.search(r"^version: (\S+)$", manifest, re.M).group(1)
    assert plugin.auth.USER_AGENT == f"grounder-hermes/{version}"
    _save(plugin, expires_in=0)
    plugin.GrounderWebSearchProvider().search("q")
    assert {r["headers"]["User-Agent"] for r in stub.requests} == {plugin.auth.USER_AGENT}


def test_token_near_expiry_is_refreshed_before_the_search(plugin, stub):
    _save(plugin, expires_in=30)
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"]
    assert stub.refreshes == 1
    assert [r["path"] for r in stub.requests] == ["/oauth/token", "/v1/search"]
    assert _searches(stub)[0]["headers"]["Authorization"] == "Bearer fresh-1"
    saved = plugin.auth.load_tokens()
    assert saved["access_token"] == "fresh-1" and saved["refresh_token"] == "refresh-1"
    assert stat.S_IMODE(os.stat(plugin.auth.token_path()).st_mode) == 0o600
    form = urllib.parse.parse_qs(_token_calls(stub)[0]["body"])
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["refresh-0"]
    assert form["scope"] == ["search"] and form["client_id"] == ["client-1"]
    assert form["resource"] == [f"{stub.origin}/v1/search"]


def test_refused_token_is_refreshed_and_the_search_sent_once_more(plugin, stub):
    _save(plugin, access="stale-401")
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"]
    assert [r["headers"]["Authorization"] for r in _searches(stub)] == [
        "Bearer stale-401", "Bearer fresh-1"]


def test_token_refused_after_refresh_is_reported_not_retried_again(plugin, stub):
    _save(plugin, access="stale-401")
    stub.rejected.add("fresh-1")
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result == {"success": False, "error":
                      "Grounder rejected the credentials. Run `hermes grounder login`."}
    assert len(_searches(stub)) == 2 and stub.refreshes == 1


def test_dead_grant_asks_for_login_without_searching(plugin, stub):
    _save(plugin, expires_in=0)
    stub.refresh_error = "invalid_grant"
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result == {"success": False,
                      "error": "Grounder sign-in has expired. Run `hermes grounder login`."}
    assert _searches(stub) == []


def test_concurrent_searches_share_one_refresh(plugin, stub):
    # Refresh tokens are single-use: a second refresh with the same one would end the grant.
    _save(plugin, expires_in=0)
    provider = plugin.GrounderWebSearchProvider()
    results = []
    threads = [threading.Thread(target=lambda: results.append(provider.search("q")))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(r["success"] for r in results) and len(results) == 8
    assert stub.refreshes == 1
    assert {r["headers"]["Authorization"] for r in _searches(stub)} == {"Bearer fresh-1"}


def test_key_in_environment_wins_and_is_never_refreshed(plugin, stub, monkeypatch):
    _save(plugin)
    monkeypatch.setenv("GROUNDER_API_KEY", "legacy-key")
    provider = plugin.GrounderWebSearchProvider()
    assert provider.is_available()
    assert provider.search("q")["success"]
    assert _searches(stub)[0]["headers"]["Authorization"] == "Bearer legacy-key"
    stub.rejected.add("legacy-key")
    assert provider.search("q") == {"success": False, "error":
                                    "Grounder rejected the credentials. Run `hermes grounder login`."}
    assert _token_calls(stub) == [] and len(_searches(stub)) == 2


def test_http_errors_become_readable_messages(plugin, stub):
    _save(plugin)
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


def test_unreachable_api_is_reported_not_raised(plugin, stub, monkeypatch):
    _save(plugin)
    monkeypatch.setattr(plugin.auth, "ORIGIN", "http://127.0.0.1:9")
    result = plugin.GrounderWebSearchProvider().search("q")
    assert not result["success"] and result["error"].startswith("Could not reach Grounder")


def test_login_saves_a_private_search_only_grant(plugin, stub, monkeypatch, capsys):
    opened = []

    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        opened.append(query)
        callback = (f"{query['redirect_uri'][0]}?code=the-code"
                    f"&state={urllib.parse.quote(query['state'][0])}")
        threading.Thread(target=_visit, args=(callback,), daemon=True).start()
        return True

    monkeypatch.setattr(plugin.auth.webbrowser, "open", browser)
    path = plugin.auth.login()
    query = opened[0]
    assert query["scope"] == ["search"] and query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [f"{stub.origin}/v1/search"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert len(query["state"][0]) >= 32
    register = json.loads(stub.requests[0]["body"])
    assert register["client_name"] == "Hermes Agent"
    assert register["token_endpoint_auth_method"] == "none"
    assert register["redirect_uris"] == query["redirect_uri"]
    form = urllib.parse.parse_qs(stub.requests[1]["body"])
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["the-code"]
    assert form["redirect_uri"] == query["redirect_uri"]
    assert len(form["code_verifier"][0]) >= 43
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "first" and saved["client_id"] == "client-1"
    assert saved["scope"] == "search" and saved["resource"] == f"{stub.origin}/v1/search"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    printed = capsys.readouterr().out
    assert "first" not in printed and "refresh-0" not in printed


def test_login_refuses_a_callback_with_the_wrong_state(plugin, stub, monkeypatch):
    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        callback = f"{query['redirect_uri'][0]}?code=forged&state=not-the-state"
        threading.Thread(target=_visit, args=(callback,), daemon=True).start()
        return True

    monkeypatch.setattr(plugin.auth.webbrowser, "open", browser)
    try:
        plugin.auth.login()
    except plugin.auth.AuthError as error:
        assert str(error) == "Sign-in was denied or the callback was invalid"
    else:
        raise AssertionError("a forged callback signed in")
    assert _token_calls(stub) == []
    assert not plugin.auth.token_path().exists()


def test_logout_deletes_the_grant_and_reports_when_there_was_none(plugin):
    _save(plugin)
    assert plugin.auth.logout()
    assert not plugin.auth.token_path().exists()
    assert not plugin.auth.logout()


def test_status_names_the_credential_and_never_prints_a_token(plugin, capsys, monkeypatch):
    assert plugin._status() == 1
    assert capsys.readouterr().out == "Not signed in. Run `hermes grounder login`.\n"
    _save(plugin, access=SECRET_TOKEN)
    assert plugin._status() == 0
    printed = capsys.readouterr().out
    assert printed.startswith("Signed in to Grounder (access token valid for 59 min)")
    assert SECRET_TOKEN not in printed and "refresh-0" not in printed
    _save(plugin, access=SECRET_TOKEN, expires_in=-10)
    assert plugin._status() == 0
    assert "access token renews on the next search" in capsys.readouterr().out
    monkeypatch.setenv("GROUNDER_API_KEY", "legacy-key")
    assert plugin._status() == 0
    printed = capsys.readouterr().out
    assert printed == "Using the Grounder search key in GROUNDER_API_KEY.\n"
    monkeypatch.delenv("GROUNDER_API_KEY")
    plugin.auth.token_path().write_text("{")
    assert plugin._status() == 1
    assert "unreadable" in capsys.readouterr().out


def test_command_exit_codes(plugin, capsys, monkeypatch):
    def failed_login(**_kwargs):
        raise plugin.auth.AuthError("Sign-in timed out")

    monkeypatch.setattr(plugin.auth, "login", failed_login)
    login = types.SimpleNamespace(grounder_command="login", no_browser=True, port=8765)
    assert plugin._command(login) == 1
    assert capsys.readouterr().out == "Sign-in timed out\n"
    assert plugin._command(types.SimpleNamespace(grounder_command="logout")) == 0
    assert capsys.readouterr().out == "Not signed in.\n"
    assert plugin._command(types.SimpleNamespace(grounder_command=None)) == 1
    assert "hermes grounder <login|status|logout>" in capsys.readouterr().out


def test_register_adds_the_backend_and_the_command(plugin):
    calls = {}

    class Context:
        def register_web_search_provider(self, provider):
            calls["provider"] = provider

        def register_cli_command(self, **kwargs):
            calls["command"] = kwargs

    plugin.register(Context())
    assert calls["provider"].name == "grounder"
    assert calls["command"]["name"] == "grounder"
    assert calls["command"]["handler_fn"] is plugin._command


def test_setup_entry_asks_for_no_key(plugin):
    entry = plugin.GrounderWebSearchProvider().get_setup_schema()
    assert entry["name"] == "Grounder" and entry["badge"] == "account"
    assert entry["env_vars"] == []
    assert "`hermes grounder login`" in entry["tag"] and "Search only" in entry["tag"]
