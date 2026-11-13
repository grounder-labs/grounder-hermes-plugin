# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""What an adversarial test of plugin 0.5.14 in a real Hermes found (GRO-832, plugin 0.5.15). Each test fails on 0.5.14."""

import json
import sys
import threading
import types
import urllib.parse

import pytest

from conftest import PLUGIN_DIR
from test_plugin import PAGE_A, PAGE_B, PAGE_C, RESULT, ROUTED, SIGN_IN, _page, _routed, _searches

FAKE_KEY = "sk-ant-api03-" + "A" * 40  # a fake: only the prefix Hermes's guard knows


def _hook(provider, query, result, turn="t", session="s", **kw):
    return provider.after_tool("web_search", {"query": query}, json.dumps(result), session_id=session, turn_id=turn, **kw)


# --- 1: a failure the user must act on is a result, so Hermes's keyless ring does not serve another vendor's rows in its place ---------------------

def test_the_provider_opts_out_of_hermess_keyless_rescue_where_hermes_honours_it(plugin):
    assert plugin.GrounderWebSearchProvider.keyless_rescue is False


@pytest.mark.parametrize("status,code", [(429, "daily_limit_reached"), (503, "capacity_reached"), (429, "cap_reached"), (401, "unauthorized")])
def test_every_answer_the_user_must_act_on_reaches_them_as_a_result_for_a_search_and_a_read(plugin, stub, keyed, status, code):
    stub.search_status, stub.search_body = status, {"error": {"code": code, "message": "Grounder's own words about it."}}
    provider = plugin.GrounderWebSearchProvider()
    search = provider.search("q")
    assert search["success"] is True and search["data"][plugin.provider.NOTICE_KEY] == "act"
    assert "Grounder's own words about it." in search["data"]["web"][0]["description"] or status == 401
    [page] = provider.extract([PAGE_A])
    assert "error" not in page and page["content"] and page["url"] != PAGE_A  # not filed under the page, so Hermes caches nothing as its text


@pytest.mark.parametrize("status,body", [(500, {"error": {"code": "internal_error", "message": "boom"}}), (502, b"<html>bad gateway</html>"),
                                         (429, {"error": {"code": "rate_limited", "message": "slow down"}}), (503, {"error": {"code": "providers_failed", "message": "none"}})])
def test_a_transient_failure_is_still_a_failure_for_hermes_to_serve_from_its_ring(plugin, stub, keyed, status, body):
    stub.search_status, stub.search_body = status, body
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"] is False and result["error"]


def test_an_act_notice_tells_the_model_to_use_other_words_once_the_user_has_acted_since_hermes_keeps_it_twenty_minutes(plugin, stub, keyed, monkeypatch):
    # Hermes keeps every successful search result for twenty minutes by query and publishes no way to drop one, so the plugin does not reach into its cache (the catalog's
    # rule 9 names ``tools.web_result_cache`` a core internal): the notice itself says how a retry gets past it. Nothing of Hermes's cache module is imported.
    monkeypatch.setitem(sys.modules, "tools.web_result_cache", None)
    stub.search_status, stub.search_body = 429, {"error": {"code": "daily_limit_reached", "message": "x", "details": {"signup_url": "https://grounder.net/account"}}}
    provider = plugin.GrounderWebSearchProvider()
    notice = provider.search("Q", 3)
    row = notice["data"]["web"][0]
    assert notice["data"][plugin.provider.NOTICE_KEY] == "act" and notice["success"] is True
    assert "the same search fails again until they have" in row["description"]
    assert "Hermes keeps this message for twenty minutes, so once they have, search again with the question in other words." in row["description"]
    assert _hook(provider, "Q", notice) is None  # a notice is no source and gets no notes
    # A refusal of this call is deterministic and its text asks for nothing: it says nothing of other words.
    refused = provider.search(f"https://example.com/?k={FAKE_KEY}")
    assert refused["data"][plugin.provider.NOTICE_KEY] == "refused" and "other words" not in refused["data"]["web"][0]["description"]
    assert _hook(provider, f"https://example.com/?k={FAKE_KEY}", refused) is None


# --- 2: an answer cites only its own turn's results (GRO-835, reversing 0.5.15's per-chat numbers) ----------------------------------------------------

def test_a_follow_up_that_searched_nothing_cites_nothing_by_number_and_gets_no_list(plugin, stub, keyed, in_chat):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A, PAGE_B)
    _hook(provider, "one", provider.search("one"), turn="t1")
    first = provider.after_answer("First [1] [2].", session_id="s", turn_id="t1")
    assert f"- Title a — {PAGE_A}\n- Title b — {PAGE_B}" in first
    # Turn 2 made no search: whatever numbers it writes name nothing, so no list and no card line.
    assert provider.after_answer("As said, [2].", session_id="s", turn_id="t2") is None
    # The first turn's results were not kept for it, whichever order the turns are answered in.
    assert provider.after_answer("As said, [1].", session_id="s", turn_id="t1") is None


def test_a_follow_up_that_searches_starts_again_at_1_and_lists_only_its_own_results(plugin, stub, keyed, in_chat):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A, PAGE_B)
    _hook(provider, "one", provider.search("one"), turn="t1")
    provider.after_answer("First [1] [2].", session_id="s", turn_id="t1")
    stub.search_body = _routed(PAGE_C, PAGE_A)
    second = json.loads(_hook(provider, "two", provider.search("two"), turn="t2"))
    assert [row["position"] for row in second["data"]["web"]] == [1, 2]
    answer = provider.after_answer("Now [1] and [2]; the earlier [3] is not here.", session_id="s", turn_id="t2")
    assert answer.split("\n\nSearched")[0] == f"Now [1] and [2]; the earlier [3] is not here.\n\nSources:\n- Title c — {PAGE_C}\n- Title a — {PAGE_A}"
    # Page B was turn 1's, and a number turn 1 gave it names nothing in turn 2.
    assert PAGE_B not in answer


def test_the_citation_note_tells_the_model_to_cite_only_this_answers_results_and_never_an_earlier_number(plugin):
    note = plugin.provider.CITE_REQUEST
    assert "Positions belong to this answer alone" in note and "start again at [1] in the next answer that searches" in note
    assert "an answer that made no search cites nothing by number, and never reuses a number from an earlier answer" in note and "search again when you need a source" in note
    assert "in every later one in this chat" not in note and "whole chat" not in note
    # The note for a host that names no turn is the MCP server's own words (the model writes the list, with links), and is not a number.
    assert "[" not in plugin.provider.CITE_REQUEST_LISTED


def test_two_chats_keep_their_results_apart_and_the_memory_of_each_turn_is_bounded(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    stub.search_body = _routed(PAGE_A, PAGE_B)
    _hook(provider, "q", provider.search("q"), session="one", turn="t", conversation_id="one")
    stub.search_body = _routed(PAGE_C)
    _hook(provider, "q2", provider.search("q2"), session="two", turn="t", conversation_id="two")
    # The same turn id in another session is another turn: each answer lists only its own session's results, and nothing is kept once it is answered.
    two = provider.after_answer("[1]", session_id="two", turn_id="t")
    assert f"Title c — {PAGE_C}" in two and "Title a" not in two and "Title b" not in two
    one = provider.after_answer("[1] [2]", session_id="one", turn_id="t")
    assert "Title a" in one and "Title b" in one and "Title c" not in one
    assert provider.after_answer("[1]", session_id="one", turn_id="t") is None
    for turn in range(plugin.provider.REMEMBERED_TURNS + 40):
        stub.search_body = _routed(PAGE_A)
        _hook(provider, f"q{turn}", provider.search(f"q{turn}"), session="big", turn=f"t{turn}", conversation_id="big")
    assert len(provider._sources) <= plugin.provider.REMEMBERED_TURNS and len(provider._turns) <= plugin.provider.REMEMBERED_TURNS


# --- 3: only a heading or a label line is a Sources list the model wrote ---------------------------------------------------------------------------

@pytest.mark.parametrize("written", ["Sources", "## Sources", "**Sources**", "**Sources:** [1] A — https://example.com/a", "Sources:", "References:\n- a", "### Citations"])
def test_a_list_the_model_wrote_under_a_heading_or_label_is_left_alone(plugin, stub, keyed, written):
    stub.search_body = _routed(PAGE_A)
    provider = plugin.GrounderWebSearchProvider()
    _hook(provider, "q", provider.search("q"))
    text = f"It is so [1].\n\n{written}"
    assert "\nSources:\n- Title a" not in provider.after_answer(text, session_id="s", turn_id="t")


@pytest.mark.parametrize("sentence", ["Sources differ slightly on the day [1].", "References to it are scarce [1].", "Citations of the paper vary [1]."])
def test_a_sentence_that_starts_with_the_word_does_not_suppress_the_list(plugin, stub, keyed, sentence):
    stub.search_body = _routed(PAGE_A)
    provider = plugin.GrounderWebSearchProvider()
    _hook(provider, "q", provider.search("q"))
    assert f"Sources:\n- Title a — {PAGE_A}" in provider.after_answer(sentence, session_id="s", turn_id="t")


# --- 4: code is not a citation ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Use `items[2]` to get the third item.", "```python\nx = items[1]\n```\nThat reads it.", "Use ``items[1]`` or ~~~\nitems[1]\n~~~ ok.",
                                  "An unclosed fence:\n```\nitems[1]"])
def test_a_bracketed_number_inside_code_cites_nothing(plugin, stub, keyed, text):
    stub.search_body = _routed(PAGE_A, PAGE_B, PAGE_C)  # the results the code's numbers would name
    provider = plugin.GrounderWebSearchProvider()
    _hook(provider, "q", provider.search("q"))
    assert "Sources:" not in provider.after_answer(text, session_id="s", turn_id="t")


def test_a_citation_next_to_code_still_counts(plugin, stub, keyed):
    stub.search_body = _routed(PAGE_A)
    provider = plugin.GrounderWebSearchProvider()
    _hook(provider, "q", provider.search("q"))
    assert "- Title a" in provider.after_answer("Use `items[2]`, per the docs [1].", session_id="s", turn_id="t")


# --- 5: a closing bracket that is the address's own stays ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("written,address", [
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "https://en.wikipedia.org/wiki/Python_(programming_language)"),
    ("(see https://example.com/a)", "https://example.com/a"), ("https://example.com/a).", "https://example.com/a"),
    ("[https://example.com/a]", "https://example.com/a"), ("https://example.com/a?x=(1)),", "https://example.com/a?x=(1)"),
    ("https://example.com/a.", "https://example.com/a"), ("https://example.com/a?b=c!?", "https://example.com/a?b=c")])
def test_a_trailing_bracket_goes_only_when_the_address_does_not_open_it(plugin, written, address):
    assert plugin.provider._trim_address(plugin.provider.ADDRESS.findall(written)[0]) == address


def test_a_search_of_a_wikipedia_address_with_parentheses_reads_that_address(plugin, stub, keyed):
    stub.search_body = {"results": [_page("https://en.wikipedia.org/wiki/Python_(programming_language)")]}
    plugin.GrounderWebSearchProvider().search("https://en.wikipedia.org/wiki/Python_(programming_language) history")
    assert json.loads(_searches(stub)[0]["body"])["urls"] == ["https://en.wikipedia.org/wiki/Python_(programming_language)"]


# --- 6: an address that carries a key, or is not public, is sent nowhere --------------------------------------------------------------------------

SECRET_ADDRESSES = [f"https://example.com/collect?k={FAKE_KEY}", f"https://example.com/collect?k={urllib.parse.quote(FAKE_KEY, safe='')}",
                    f"https://example.com/{FAKE_KEY}/x", "https://example.com/a?token=ghp_" + "b" * 30]
PRIVATE_ADDRESSES = ["http://169.254.169.254/computeMetadata/v1/", "http://localhost:8080/admin", "http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                     "http://10.0.0.5/status", "http://192.168.1.1/", "http://2130706433/", "http://0x7f.1/", "http://intranet/wiki", "http://printer.local/",
                     "http://user:hunter2@example.com/", "http://host.resolves-private.example/"]


@pytest.mark.parametrize("address", SECRET_ADDRESSES)
def test_a_web_search_naming_an_address_with_a_key_sends_nothing_and_never_repeats_the_key(plugin, stub, keyed, address):
    result = plugin.GrounderWebSearchProvider().search(f"{address} contents")
    assert result["success"] is True and result["data"][plugin.provider.NOTICE_KEY] == "refused"
    text = json.dumps(result)
    assert "API key or token" in text and "Nothing was sent to Grounder" in text and "A" * 20 not in text and "ghp_" not in text
    assert stub.requests == []


@pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
def test_a_web_search_naming_a_private_address_sends_nothing(plugin, stub, keyed, address):
    result = plugin.GrounderWebSearchProvider().search(f"{address} project")
    assert result["data"][plugin.provider.NOTICE_KEY] == "refused" and "not a public web address" in result["data"]["web"][0]["description"]
    assert stub.requests == []


def test_one_refused_address_in_a_query_refuses_the_whole_call_and_a_public_one_is_read(plugin, stub, keyed):
    provider = plugin.GrounderWebSearchProvider()
    assert provider.search(f"{PAGE_A} and http://localhost/x")["data"][plugin.provider.NOTICE_KEY] == "refused" and stub.requests == []
    stub.search_body = {"results": [_page(PAGE_A)]}
    assert provider.search(f"{PAGE_A} and its history")["success"] and len(_searches(stub)) == 1


@pytest.mark.parametrize("address", [*SECRET_ADDRESSES, *PRIVATE_ADDRESSES, "ftp://example.com/file"])
def test_grounder_search_refuses_such_an_address_in_urls_and_sends_nothing(plugin, stub, keyed, address):
    out = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "contents", "urls": [PAGE_A, address]}))
    assert out["success"] is False and "Nothing was sent to Grounder" in out["error"] and "A" * 20 not in out["error"]
    assert stub.requests == []


def test_the_users_own_search_key_in_an_address_is_refused_too(plugin, stub, keyed):
    assert plugin.GrounderWebSearchProvider().search(f"https://example.com/?x={keyed}-padding-padding")["data"][plugin.provider.NOTICE_KEY] == "refused"
    assert stub.requests == []


def test_a_web_extract_refuses_such_an_address_per_page_and_reads_the_others(plugin, stub, keyed):
    stub.search_body = {"results": [_page(PAGE_A)]}
    rows = plugin.GrounderWebSearchProvider().extract([f"https://example.com/c?k={FAKE_KEY}", PAGE_A, "http://localhost/"])
    assert rows[0]["error"].startswith("Blocked:") and rows[1]["content"] == "page text" and rows[2]["error"].startswith("Blocked:")
    assert json.loads(_searches(stub)[0]["body"])["urls"] == [PAGE_A]
    stub.requests.clear()
    assert all(row["error"] for row in plugin.GrounderWebSearchProvider().extract(["http://localhost/", "http://10.0.0.1/"])) and stub.requests == []


def test_without_hermess_own_checks_the_plugins_own_still_refuse(plugin, stub, keyed, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.url_safety", None)  # an import of it raises ImportError, as in a Hermes without it
    provider = plugin.GrounderWebSearchProvider()
    for address in (f"https://example.com/?k={FAKE_KEY}", "http://127.0.0.1/", "http://user:pw@example.com/"):
        assert provider.search(address)["data"][plugin.provider.NOTICE_KEY] == "refused"
    assert stub.requests == []


def test_an_ordinary_address_that_merely_looks_like_a_key_prefix_is_read(plugin, stub, keyed, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.url_safety", None)
    stub.search_body = {"results": [_page("https://example.com/blog/sk-learn")]}
    assert plugin.GrounderWebSearchProvider().search("https://example.com/blog/sk-learn what is it")["data"].get(plugin.provider.NOTICE_KEY) is None
    assert len(_searches(stub)) == 1


# --- 7: one session's words and results never reach another's ----------------------------------------------------------------------------------

def test_one_sessions_search_words_never_go_out_with_another_sessions_page_read(plugin, stub, keyed, monkeypatch):
    provider = plugin.GrounderWebSearchProvider()
    mine = plugin.provider
    stub.search_body = {"results": [{**RESULT, "url": PAGE_A, "title": "A", "text": "the passage A's search showed"}]}
    monkeypatch.setenv("HERMES_SESSION_ID", "user-a")
    mine.note_turn(session_id="user-a", conversation_id="conv-a", turn_id="a1", tool_name="web_search")
    provider.search("A's private query words")
    monkeypatch.setenv("HERMES_SESSION_ID", "user-b")
    mine.note_message(session_id="user-b", user_message="B's own question")
    mine.note_turn(session_id="user-b", conversation_id="conv-b", turn_id="b1", tool_name="web_extract")
    stub.search_body = {"results": []}
    rows = provider.extract([PAGE_A])
    sent = json.loads(_searches(stub)[-1]["body"])
    assert sent["query"] == "B's own question" and "private" not in json.dumps(sent)
    # B's failed read does not get A's snippet as a fallback, and is not Grounder's to number.
    assert rows[0]["error"] and "passage" not in json.dumps(rows)
    page = json.dumps({"results": [{"url": PAGE_A, "title": "A", "content": "x", "error": None}]})
    assert provider.after_tool("web_extract", {}, page, session_id="user-b", turn_id="b1", conversation_id="conv-b") is None
    # A keeps what A found.
    monkeypatch.setenv("HERMES_SESSION_ID", "user-a")
    stub.search_body = {"results": []}
    got = provider.extract([PAGE_A])
    assert json.loads(_searches(stub)[-1]["body"])["query"] == "A's private query words" and "passage" in got[0]["content"]


# --- 9, 10, 11: a free search drops what Grounder refuses it; each way of searching has its own words for a 401 --------------------------------

def test_a_free_search_says_what_it_did_not_send_in_the_words_it_was_asked_in(plugin, stub, free):
    out = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q", "workspace": "team"}))
    assert out["note"].startswith("No Grounder account is connected, so workspace was not sent and this ran as a free search.")
    assert "workspace" not in json.loads(_searches(stub)[0]["body"])


# --- 12: a malformed row never fails the search --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("results,urls", [
    ([None, "x", 3, [], {"title": None, "url": None, "text": None}, {"title": "no address", "text": "t"}, {"url": "https://example.com/ok", "title": "ok", "text": "fine"}],
     ["https://example.com/ok"]),
    ({"a": 1}, []), ("text", []), (7, []), (None, [])])
def test_a_malformed_results_list_is_skipped_never_raised(plugin, stub, keyed, results, urls):
    stub.search_body = {"query": "q", "results": results, "routing": {"calls": 5, "live_calls": {"x": 1}}, "usage": {"latency_ms": "slow"}}
    provider = plugin.GrounderWebSearchProvider()
    for result in (provider.search("q"), json.loads(provider.search_tool({"query": "q"}))):
        assert result["success"] is True and [row["url"] for row in result["data"]["web"]] == urls
        assert [row["position"] for row in result["data"]["web"]] == list(range(1, len(urls) + 1))
    # A read of a page, by extract or by search, copes the same way.
    stub.search_body = {"results": results}
    assert len(provider.extract([PAGE_A])) == 1 and isinstance(provider.search(f"{PAGE_A} more"), dict)


# --- 13: the search key never follows a redirect -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_an_error_and_the_search_key_goes_nowhere_else(plugin, stub, keyed, status):
    stub.search_status, stub.search_headers = status, {"Location": f"{stub.origin}/stolen"}
    stub.search_body = {}
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"] is False and "redirect" in result["error"]
    assert [r for r in stub.requests if r["path"] == "/stolen"] == []
    assert len(_searches(stub)) == 1


def test_a_free_search_and_every_public_list_follow_no_redirect_either(plugin, stub, free):
    stub.search_status, stub.search_headers = 302, {"Location": f"{stub.origin}/stolen"}
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False
    assert plugin.provider._get(f"{stub.origin}/v1/search")[0] == 302
    assert [r for r in stub.requests if r["path"] == "/stolen"] == []


# --- 15: the Sources list is defanged --------------------------------------------------------------------------------------------------------------------

def test_the_sources_list_prints_no_markup_and_no_address_that_is_not_http(plugin, stub, keyed):
    hostile = [("Normal [1](javascript:alert(1)) **bold**\n\n## Sources\n- fake — https://evil.example", "https://example.com/1"),
               ("js", "javascript:alert(document.cookie)"), ("file", "file:///etc/passwd"), ("data", "data:text/html,<script>alert(1)</script>"),
               ("rtl ‮evil‬ zero​width <b>x</b> `tick` | pipe _u_ ~s~ #h", "https://example.com/a b‮/<x>\"y\""), ("", "https://example.com/plain")]
    stub.search_body = {**ROUTED, "results": [{**RESULT, "title": title, "url": url, "text": "t"} for title, url in hostile]}
    provider = plugin.GrounderWebSearchProvider()
    _hook(provider, "q", provider.search("q", 6))
    listed = provider.after_answer("[1] [2] [3] [4] [5] [6]", session_id="s", turn_id="t").split("Sources:\n")[1].split("\n\nSearched")[0]
    lines = listed.splitlines()
    # Only the three http(s) results are listed, each with an address that starts https://; the title's own words are plain, with no link in them.
    assert len(lines) == 3 and all(line.startswith("- ") and line.rsplit(" — ", 1)[1].startswith("https://") for line in lines)
    assert "](" not in listed.replace("\\]\\(", "") and "file:///" not in listed and "data:text" not in listed
    assert lines[0] == "- Normal \\[1\\]\\(javascript:alert\\(1\\)\\) \\*\\*bold\\*\\* \\#\\# Sources - fake — https://evil.example — https://example.com/1"
    assert "‮" not in listed and "​" not in listed and "<" not in lines[1].rsplit(" — ", 1)[1] and " " not in lines[1].rsplit(" — ", 1)[1]
    assert lines[1].startswith("- rtl evil zerowidth \\<b\\>x\\</b\\> \\`tick\\` \\| pipe \\_u\\_ \\~s\\~ \\#h — https://example.com/a%20b%E2%80%AE/%3Cx%3E%22y%22")
    assert lines[2] == "- https://example.com/plain — https://example.com/plain"


# --- 8: nothing tells the user to set the sign-in client by hand -----------------------------------------------------------------------------------

def test_no_doc_tells_the_user_to_set_the_sign_in_client(plugin):
    for name in ("README.md", "after-install.md", "plugin.yaml"):
        text = (PLUGIN_DIR / name).read_text()
        assert "config set GROUNDER_OAUTH_CLIENT_ID" not in text, name
    assert "Set it to" not in (PLUGIN_DIR / "plugin.yaml").read_text()
