# Copyright 2026 Grounder Labs, Inc
# SPDX-License-Identifier: Apache-2.0

"""A read done on this Mac, told to the account (GRO-989): the prompt and each page's host and path, never page text, posted by the plugin itself with its own key, and only while the
account's saved setting says ``mac_reads.record`` is true. A Mac with no account sends nothing, and the reader binary never holds a key. The reads and the account are stand-ins."""

import io
import json
import time

import pytest
from test_direct_dev import PAGE, dev_on  # noqa: F401  (the fixture and the address the direct tests read)


@pytest.fixture
def told(plugin, monkeypatch):
    """The account's answer to 'record reads?' and every report it was sent; the work that tells it runs at once, not on a thread."""
    state = {"record": True, "sent": []}
    pr = plugin.provider
    pr._mac_reads_cache.update(at=-1e9, value=False)
    monkeypatch.setattr(plugin.direct_dev, "spawn", lambda work: work())
    monkeypatch.setattr(pr, "_settings_call", lambda method, body=None: ({"search": {"mode": "grounder-direct"}, "mac_reads": {"record": state["record"]}}, None))
    monkeypatch.setattr(plugin.direct_dev, "read_sender", lambda body, timeout: state["sent"].append(body) or True)
    return state


def test_a_read_of_a_named_page_is_told_to_the_account_as_host_and_path(plugin, hermes, dev_on, reads, told):
    plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}?utm=secret#frag", limit=5)
    assert told["sent"] == [{"prompt": "what is new on", "pages": [PAGE]}]
    assert "secret" not in json.dumps(told["sent"]) and "posts" not in json.dumps(told["sent"])  # no query, fragment or page text


def test_an_extract_is_told_too(plugin, hermes, dev_on, reads, told):
    plugin.GrounderWebSearchProvider().extract([PAGE])
    assert [body["pages"] for body in told["sent"]] == [[PAGE]]


def test_nothing_is_sent_while_the_account_says_not_to_record(plugin, hermes, dev_on, reads, told):
    told["record"] = False
    plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}", limit=5)
    assert told["sent"] == []


def test_a_mac_with_no_account_sends_nothing(plugin, hermes, dev_on, reads, told, monkeypatch):
    def not_signed_in(method, body=None):
        raise plugin.provider.ProviderNotSignedIn("no")

    monkeypatch.setattr(plugin.provider, "_settings_call", not_signed_in)
    plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}", limit=5)
    assert told["sent"] == []


def test_an_address_the_plugin_refused_is_never_told(plugin, hermes, dev_on, reads, told):
    plugin.GrounderWebSearchProvider().search("read http://localhost:8080/admin and http://resolves-private.example/x", limit=5)
    assert told["sent"] == []


def test_the_setting_is_read_once_a_minute_not_once_a_read(plugin, hermes, dev_on, reads, told, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin.provider, "_settings_call", lambda method, body=None: calls.append(method) or ({"search": {}, "mac_reads": {"record": True}}, None))
    plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}", limit=5)
    after_one = len(calls)
    for _ in range(2):
        plugin.GrounderWebSearchProvider().search(f"what is new on {PAGE}", limit=5)
    assert len(calls) == after_one and len(told["sent"]) == 3  # the later reads were told without asking the account again


def test_the_body_is_cut_and_bounded(plugin):
    dd = plugin.direct_dev
    assert dd.read_body("", [PAGE]) is None and dd.read_body("q", []) is None and dd.read_body("q", ["not an address", "ftp://x.example/a"]) is None
    body = dd.read_body("see https://Example.com:8443/a/b?x=1#y now", ["https://user:pw@Example.com:8443/a/b?x=1#y", "https://example.com/a/b"] + [f"https://example.org/{i}" for i in range(12)])
    assert body["prompt"] == "see https://example.com/a/b now" and body["pages"][0] == "https://example.com/a/b" and len(body["pages"]) == 10
    assert body["pages"].count("https://example.com/a/b") == 1


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_the_report_is_a_post_with_the_plugins_own_key_and_the_reader_never_gets_one(plugin, monkeypatch):
    pr = plugin.provider
    seen = []
    monkeypatch.setenv("GROUNDER_API_KEY", "gr-test-key")
    monkeypatch.setattr(pr, "_open", lambda request, timeout: seen.append((request, timeout)) or _Response(b'{"recorded":true}'))
    assert pr._read_call({"prompt": "p", "pages": [PAGE]}, 3) is True
    request, timeout = seen[0]
    assert request.get_method() == "POST" and request.full_url == "https://api.grounder.net/v1/direct/reads" and timeout == 3
    assert request.get_header("Authorization") == "Bearer gr-test-key" and json.loads(request.data) == {"prompt": "p", "pages": [PAGE]}
    env = plugin.direct_dev.reader_env()
    assert not [name for name in env if "KEY" in name or "TOKEN" in name], env
    assert "gr-test-key" not in json.dumps(env)


def test_the_setting_answer_is_read_for_mac_reads_and_kept(plugin, monkeypatch):
    pr = plugin.provider
    pr._mac_reads_cache.update(at=-1e9, value=False)
    answers = [({"search": {}, "mac_reads": {"record": True}}, None), ({"search": {}}, None)]
    monkeypatch.setattr(pr, "_settings_call", lambda method, body=None: answers.pop(0))
    assert pr._mac_reads_recording() is True and pr._mac_reads_recording() is True  # kept
    pr._mac_reads_cache.update(at=-1e9)
    assert pr._mac_reads_recording() is False  # a server that says nothing about it is a server that does not record
    assert time.monotonic() - pr._mac_reads_cache["at"] < 5
