# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""Grounder direct as an add-on, with no page named: the search runs in the account's mode with ``direct: "mac"``, then the top page (full-page, summary) or the top three
sites' pages (highlights) are read directly, their rows first and the search's rows after. The account's saved ``direct`` wins over this Mac's setting. ``site`` finds a page
named without its link."""

import json

import pytest
from test_commands import Host, _labels, lists, signed_in  # noqa: F401  (fixtures)
from test_direct_dev import reads  # noqa: F401  (fixture)

NEWS = [f"https://{host}/story" for host in ("a.example", "b.example", "c.example", "d.example")]


def _account(plugin, stub, monkeypatch, **direct):
    """The account's saved setting with a ``direct`` block, as GET /v1/search/settings answers it."""
    plugin.provider._direct_cache.update(at=0.0, value=None)
    original = stub.settings_answer
    monkeypatch.setattr(stub, "settings_answer", lambda: {**original(), "direct": direct})


def _answer(stub, urls, candidates=None):
    stub.search_body = {"query": "q", "results": [{"title": f"T{i}", "url": url, "text": "snippet"} for i, url in enumerate(urls)],
                        **({"direct": {"candidates": [{"url": u, "site": u.split("/")[2]} for u in candidates]}} if candidates is not None else {})}


def _search_bodies(stub):
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search"]


pytestmark = pytest.mark.usefixtures("signed_in")


@pytest.fixture
def done(request):
    return request.getfixturevalue("reads")


@pytest.fixture
def addon(plugin, stub, monkeypatch):
    _account(plugin, stub, monkeypatch, use="add-on", format="full-page")
    return plugin.GrounderWebSearchProvider()


def test_addon_with_no_page_reads_the_top_one_in_full_page(addon, stub, done):
    _answer(stub, NEWS)
    rows = addon.search("who won the match", limit=5)["data"]["web"]
    assert done == [NEWS[0]]
    assert rows[0]["url"] == NEWS[0] and rows[0]["description"].startswith("[Grounder direct: read directly from") and rows[0]["position"] == 1
    assert [r["url"] for r in rows[1:]] == NEWS and rows[1]["position"] == 2  # the search's own rows follow


def test_addon_with_summary_also_reads_one(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on", format="summary")
    _answer(stub, NEWS)
    plugin.GrounderWebSearchProvider().search("who won", limit=5)
    assert done == [NEWS[0]]


def test_addon_with_highlights_reads_three_different_sites(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on", format="highlights")
    urls = ["https://a.example/1", "https://a.example/2", "https://b.example/1", "https://c.example/1", "https://d.example/1"]
    _answer(stub, urls)
    rows = plugin.GrounderWebSearchProvider().search("who won", limit=5)["data"]["web"]
    assert done == ["https://a.example/1", "https://b.example/1", "https://c.example/1"]
    assert [r["position"] for r in rows] == list(range(1, len(rows) + 1))


def test_a_refused_top_result_is_skipped(addon, stub, done):
    _answer(stub, ["http://localhost:8080/admin", "https://resolves-private.example/x", NEWS[1]])
    addon.search("who won", limit=5)
    assert done == [NEWS[1]]


def test_the_body_sent_carries_direct_mac(addon, stub, done):
    _answer(stub, NEWS)
    addon.search("who won", limit=5)
    json.loads(addon.search_tool({"query": "who won"}))
    assert [b.get("direct") for b in _search_bodies(stub)] == ["mac", "mac"]


def test_the_account_setting_wins_over_the_local_one(plugin, stub, monkeypatch, done):
    plugin.direct_dev.set_state(plugin.direct_dev.OFF)
    _account(plugin, stub, monkeypatch, use="add-on")
    _answer(stub, NEWS)
    plugin.GrounderWebSearchProvider().search("who won", limit=5)
    assert done == [NEWS[0]]
    # and the other way: the account says off while this Mac says add-on
    plugin.direct_dev.set_state(plugin.direct_dev.ADDON)
    _account(plugin, stub, monkeypatch, use="off")
    del done[:]
    plugin.GrounderWebSearchProvider().search("who won", limit=5)
    assert done == [] and "direct" not in _search_bodies(stub)[-1]


def test_the_local_setting_is_the_fallback_with_no_account_block(plugin, stub, done):
    plugin.direct_dev.set_state(plugin.direct_dev.ADDON)
    _answer(stub, NEWS)
    plugin.GrounderWebSearchProvider().search("who won", limit=5)
    assert done == [NEWS[0]]


def test_a_named_page_still_reads_only_that_page(addon, stub, done):
    page = "https://www.reddit.com/r/MechanicalKeyboards/"
    rows = addon.search(f"what is new on {page}", limit=5)["data"]["web"]
    assert done == [page]
    assert [r["url"] for r in rows] == [page] and not _search_bodies(stub)


def test_the_account_format_is_used_and_a_users_own_words_override_it(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on", format="highlights")
    _answer(stub, ["https://a.example/1", "https://b.example/1", "https://c.example/1"])
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", "format": "full-page"})  # the call's own format wins
    assert done == ["https://a.example/1"]


def test_candidates_are_picked_before_results_and_a_failed_read_tries_the_next_on_the_site(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on", format="full-page")
    blocked = "https://news.example/blocked"
    _answer(stub, ["https://other.example/r"], candidates=[blocked, "https://news.example/ok", "https://news.example/ok2"])
    rows = plugin.GrounderWebSearchProvider().search("who won", limit=5)["data"]["web"]
    assert done == [blocked, "https://news.example/ok"]  # the candidate list, not the result; one try more on the same site
    assert rows[0]["url"] == "https://news.example/ok"


def test_three_failed_tries_on_one_site_say_what_each_said(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on")
    urls = [f"https://news.example/blocked{i}" for i in range(5)]
    _answer(stub, [], candidates=urls)
    rows = plugin.GrounderWebSearchProvider().search("who won", limit=5)["data"]["web"]
    assert done == urls[:3] and rows[0]["description"].count("did not read") == 3


def test_site_finds_the_page_with_a_site_scoped_search_then_reads_it(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="only", format="full-page")
    _answer(stub, ["https://elsewhere.example/x"], candidates=["https://elsewhere.example/x", "https://www.bbc.co.uk/sport/tennis/1"])
    answer = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "Alcaraz takes apart Cobolli", "site": "bbc.co.uk"}))
    (body,) = _search_bodies(stub)
    assert body["filters"]["domains"] == {"values": ["bbc.co.uk"], "mode": "include"} and body["direct"] == "mac" and "urls" not in body
    assert done == ["https://www.bbc.co.uk/sport/tennis/1"] and answer["data"]["web"][0]["url"] == "https://www.bbc.co.uk/sport/tennis/1"


def test_site_with_nothing_on_it_says_so_and_lists_what_came_back(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on")
    _answer(stub, ["https://elsewhere.example/x"], candidates=["https://elsewhere.example/x"])
    answer = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "an article", "site": "bbc.co.uk"}))
    assert done == [] and "no page on bbc.co.uk" in answer["data"]["web"][0]["description"] and "elsewhere.example/x" in answer["data"]["web"][0]["description"]


def test_a_front_page_named_in_urls_is_read_as_given_and_site_runs_a_search_only_when_passed(plugin, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on")
    _answer(stub, [], candidates=["https://www.bbc.co.uk/news/9"])
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "summarize the Alcaraz Cobolli match report", "urls": ["https://www.bbc.co.uk/"]})
    assert done == ["https://www.bbc.co.uk/"] and not _search_bodies(stub)
    del done[:]
    provider.search_tool({"query": "this", "urls": ["https://www.bbc.co.uk/news/1"], "site": "bbc.co.uk"})
    assert done == ["https://www.bbc.co.uk/news/1"] and not _search_bodies(stub)  # a page address wins over site


def test_picking_direct_saves_use_and_format_on_the_account(plugin, stub, monkeypatch):
    _account(plugin, stub, monkeypatch, use="off")
    plugin.commands.search_command(Host(), "addon on")
    plugin.commands.search_format_command(Host(), "highlights")
    puts = [json.loads(r["body"]) for r in stub.requests if r["method"] == "PUT"]
    assert puts == [{"direct": {"use": "add-on"}}, {"direct": {"format": "highlights"}}]


def test_the_hub_and_state_follow_the_account(plugin, stub, monkeypatch):
    plugin.direct_dev.set_state(plugin.direct_dev.OFF)  # this Mac says off
    _account(plugin, stub, monkeypatch, use="only", format="summary")
    assert plugin.direct_dev.state() == plugin.direct_dev.ONLY and plugin.direct_formats.saved() == "summary"
    host = Host(["done"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[0][1])[3] == "Grounder direct (current)"


def test_a_save_the_account_refuses_is_not_claimed_as_saved_on_it(plugin, hermes, stub):
    stub.settings_script = [(200, stub.settings_answer()), (400, {"error": {"code": "invalid_request", "message": "direct is not part of this key"}})]
    reply = " ".join(plugin.commands.search_command(Host(), "addon on").split())
    assert "saved on your Grounder account" not in reply
    assert "saved on this machine only: Grounder did not take it" in reply and "direct is not part of this key" in reply and "account still applies" in reply


def test_a_save_the_account_takes_says_so_and_with_no_credential_says_this_machine(plugin, hermes, stub):
    assert "(saved on your Grounder account)" in " ".join(plugin.commands.search_command(Host(), "addon on").split())
    hermes.token = None
    assert "(saved on this machine: web.grounder.direct_format in config.yaml)" in " ".join(plugin.commands.search_format_command(Host(), "summary").split())


def test_highlights_counts_one_site_by_its_registrable_domain(plugin, hermes, stub, monkeypatch, done):
    _account(plugin, stub, monkeypatch, use="add-on", format="highlights")
    _answer(stub, [], candidates=["https://news.bbc.co.uk/1", "https://www.bbc.co.uk/2", "https://x.com.au/1", "https://shop.x.com.au/2", "https://y.example/1", "https://z.example/1"])
    plugin.GrounderWebSearchProvider().search("who won", limit=5)
    assert done == ["https://news.bbc.co.uk/1", "https://x.com.au/1", "https://y.example/1"]
