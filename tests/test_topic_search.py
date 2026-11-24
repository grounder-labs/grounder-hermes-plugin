# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import json

NEWS = "https://news.example/story"
DOCS = "https://docs.example/article"


def calls(stub):
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search"]


def test_hermes_reads_candidates_by_topic_and_keeps_feed_rows(plugin, free, stub, reads, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "state", lambda: "only")
    stub.search_body = {
        "results": [{"url": "https://feed.example/score", "title": "A score", "provenance": {"topic": "sports"}}],
        "topics": [
            {"id": "news", "source": "news.example", "reached": "direct"},
            {"id": "docs", "source": "docs.example", "reached": "direct"},
            {"id": "sports", "source": "feed.example", "reached": "feed"},
        ],
        "direct": {"candidates": [{"topic": "news", "url": NEWS}, {"topic": "docs", "url": DOCS}]},
    }
    answer = plugin.GrounderWebSearchProvider().search("news and docs and sports")
    assert [row["url"] for row in answer["data"]["web"]] == [NEWS, DOCS, "https://feed.example/score"]
    assert reads == [NEWS, DOCS]
    assert calls(stub) == [{"query": "news and docs and sports", "top_k": 5, "mode": "grounder-direct", "reader": "mac", "format": "summary"}]
    assert "Found by a Grounder direct search" in answer["data"]["web"][0]["description"]
    assert "No search provider was called" not in answer["data"]["web"][0]["description"]


def test_named_page_stays_on_the_mac_and_is_free(plugin, free, stub, reads, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "state", lambda: "only")
    answer = plugin.GrounderWebSearchProvider().search(f"read {NEWS}")
    assert reads == [NEWS] and calls(stub) == []
    assert "No search provider was called" in answer["data"]["web"][0]["description"]


def test_named_site_uses_mac_reader_instead_of_the_legacy_domain_filter(plugin, free, stub, reads, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "state", lambda: "only")
    stub.search_body = {"results": [], "direct": {"candidates": [{"topic": "site", "url": NEWS}, {"topic": "site", "url": DOCS}]}}
    reply = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "Latest headlines", "site": "news.example"}))
    assert [row["url"] for row in reply["data"]["web"]] == [NEWS]
    assert reads == [NEWS]
    assert calls(stub) == [{"query": "Latest headlines", "mode": "grounder-direct", "reader": "mac", "format": "summary", "site": "news.example"}]


def test_provider_mode_skips_local_reads_even_with_direct_saved(plugin, free, stub, reads, monkeypatch):
    monkeypatch.setattr(plugin.direct_dev, "state", lambda: "only")
    plugin.GrounderWebSearchProvider().search_tool({"query": "weather", "mode": "grounder-provider"})
    assert calls(stub) == [{"query": "weather", "mode": "grounder-provider"}] and reads == []


def test_no_topic_is_a_grounder_notice_with_no_local_read_or_payment(plugin, free, stub, reads):
    stub.search_body = {"topics": [], "results": [], "no_topic": True, "usage": {"cost_usd": 0}}
    data = plugin.GrounderWebSearchProvider().search("ordinary sentence")["data"]
    assert data["grounder_notice"] == "no_topic"
    assert "nothing was charged" in data["web"][0]["description"]
    assert "grounder_payment" not in data and reads == []


def test_catalog_returns_the_public_topics_without_sending_a_key(plugin, keyed, stub):
    stub.topics_body = {"status": "decided", "topics": [{"id": "news", "name": "News"}]}
    reply = json.loads(plugin.GrounderWebSearchProvider().list_topics_tool({}))
    assert reply == {"success": True, "data": stub.topics_body}
    assert stub.requests[-1]["path"] == "/v1/topics"
    assert "Authorization" not in stub.requests[-1]["headers"]
