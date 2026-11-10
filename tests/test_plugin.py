# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from conftest import PLUGIN_DIR, manifest, yaml

RESULT = {"url": "https://example.com/a", "title": "A", "text": "first  passage\n text",
          "rank": 1, "provenance": {"found_by": ["brave"]}}
SIGN_IN = "Not signed in to Grounder. Run `hermes auth add grounder:grounder`."


def _searches(stub):
    return [r for r in stub.requests if r["path"] == "/v1/search"]


def test_without_sign_in_search_asks_for_hermes_sign_in_and_sends_nothing(plugin, stub, hermes):
    provider = plugin.GrounderWebSearchProvider()
    assert provider.name == "grounder" and provider.display_name == "Grounder"
    assert provider.supports_search() and provider.supports_extract()
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
         "description": "short", "position": 2}]}, "cite_sources": plugin.provider.CITE_REQUEST}
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

        def register_hook(self, name, callback):
            calls.setdefault("hooks", {})[name] = callback

        def register_cli_command(self, **kwargs):
            calls["command"] = kwargs

    plugin.register(Context())
    assert calls["provider"].name == "grounder"
    assert "command" not in calls
    assert calls["hooks"] == {"transform_tool_result": calls["provider"].after_tool, "transform_llm_output": calls["provider"].after_answer, "pre_tool_call": plugin.provider.note_turn}


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


def test_extract_is_one_search_for_the_pages_at_the_6k_cap_with_no_question_when_no_search_found_them(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_B), _page(PAGE_A)]}
    rows = plugin.GrounderWebSearchProvider().extract([PAGE_A, PAGE_B], format="markdown", include_raw=True, max_chars=99)
    assert len(_searches(stub)) == 1
    assert json.loads(_searches(stub)[0]["body"]) == {"query": f"{PAGE_A} {PAGE_B}", "urls": [PAGE_A, PAGE_B], "top_k": 2,
                                                      "text_max_chars": 6000, "features": ["page-text"]}
    # In the order asked, whatever order Grounder answered in.
    assert rows == [{"url": PAGE_A, "title": "T", "content": "page text", "raw_content": "page text"},
                    {"url": PAGE_B, "title": "T", "content": "page text", "raw_content": "page text"}]


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
    assert "example.com/0/0" not in provider._found_by and "example.com/119/4" in provider._found_by
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


def test_a_response_that_ignored_the_budget_is_capped_at_6000(plugin, stub, hermes):
    hermes.token = "t"
    stub.search_body = {"results": [_page(PAGE_A, text="w" * 9000)]}
    row = plugin.GrounderWebSearchProvider().extract([PAGE_A])[0]
    assert len(row["content"]) == plugin.provider.EXTRACT_CHARS == 6000 and row["content"].endswith("…")


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
    hermes.raises = plugin.provider.ProviderNotSignedIn("grant revoked")
    assert provider.extract([PAGE_A])[0]["error"] == SIGN_IN


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


def test_the_sign_in_client_counts_as_ready_so_hermes_keeps_the_web_tools(plugin, monkeypatch):
    # With no selected backend ready, Hermes offers the model neither web_search nor web_extract.
    provider = plugin.GrounderWebSearchProvider()
    assert not provider.is_available()
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
    assert ctx.providers[0].is_available()  # Hermes hides web_search and web_extract otherwise


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
    assert list(result) == ["success", "data", "cite_sources"]
    assert result["cite_sources"].startswith("Cite every result your answer uses, each with its link, in this answer and in every later one in this chat, not only the first.")
    stub.search_body = {"query": "q", "results": []}
    assert "cite_sources" not in plugin.GrounderWebSearchProvider().search("q")


def test_a_row_says_where_its_result_came_from_as_the_card_does(plugin, stub, keyed):
    stub.search_body = {"query": "q", "results": [{**RESULT, "published_date": "2026-11-05", "provenance": {
        "provider": "newsapi", "features": ["news"], "licensed": True, "licensed_basis": "robots-allowed",
        "publisher": {"name": "AP", "domain": "apnews.com"}}}, RESULT]}
    rows = plugin.GrounderWebSearchProvider().search("q")["data"]["web"]
    assert rows[0]["source"] == "AP (apnews.com) · 2026-11-05 · news via NewsAPI.ai · licensed: the site allows it"
    assert "source" not in rows[1]


def test_an_answer_that_searched_ends_with_the_card_line_once(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    assert provider.after_tool("web_search", {"query": "q"}, "{}", session_id="s", turn_id="t") is None
    assert provider.after_answer("The answer.\n", session_id="s", turn_id="t") == "The answer.\n\nSearched with Grounder · via Exa, xAI +1 · 1 source · 1.4 s"
    assert provider.after_answer("Again.", session_id="s", turn_id="t") is None


def test_every_search_of_a_turn_is_on_the_one_line(plugin, stub, keyed):
    stub.search_body = {**ROUTED, "usage": {"latency_ms": 300}, "routing": {"calls": [{"provider": "exa", "status": "ok", "result_count": 1}]}}
    provider = plugin.GrounderWebSearchProvider()
    for query in ("one", "two"):
        provider.search(query)
        provider.after_tool("web_search", {"query": query}, "{}", session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t") == "A\n\nSearched with Grounder · via Exa · 2 sources · 600 ms"


def test_no_line_for_a_turn_grounder_did_not_search(plugin, stub, keyed):
    stub.search_body = ROUTED
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.after_tool("web_search", {"query": "another backend answered this"}, "{}", session_id="s", turn_id="t")
    provider.after_tool("terminal", {"command": "ls"}, "{}", session_id="s", turn_id="t")
    assert provider.after_answer("A", session_id="s", turn_id="t") is None
    provider.after_tool("web_search", {"query": "q"}, "{}", session_id="s", turn_id="t")
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


def test_a_search_outside_a_hermes_session_sends_no_context(plugin, stub, keyed, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    plugin.GrounderWebSearchProvider().search("query")
    assert "context" not in json.loads(_searches(stub)[-1]["body"])
