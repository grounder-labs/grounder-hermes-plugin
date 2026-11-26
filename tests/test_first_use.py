# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The first answered search of an install says, once, who pays for searches: nothing, an account or a wallet (GRO-915).

The line reads the state the search really used (a key, a Hermes sign-in, none, a wallet's payment). ``web.grounder.first_use_shown`` in
Hermes's config keeps it to once: the plugin writes no file of its own.
"""

import sys

import yaml

from test_plugin import RESULT


def _note(out):
    return out["data"].get("note", "")


def _config(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


def test_with_no_account_the_first_search_says_it_is_free_and_how_to_sign_in_and_the_next_says_nothing(first_use, stub, free, tmp_path):
    stub.search_body = {"query": "q", "results": [RESULT]}
    provider = first_use.GrounderWebSearchProvider()
    first = _note(provider.search("q"))
    assert "free with no account" in first and "hermes auth add grounder:grounder" in first
    assert _config(tmp_path)["web"]["grounder"]["first_use_shown"] is True
    assert _note(provider.search("q")) == ""


def test_a_new_process_does_not_say_it_again(first_use, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"web": {"grounder": {"first_use_shown": True, "share_usage_context": True}}}))
    assert first_use.provider._first_use_line("free", False) == ""
    assert _config(tmp_path)["web"]["grounder"]["share_usage_context"] is True


def test_a_signed_in_search_says_it_runs_on_the_account_and_no_sign_in_command(first_use, stub, hermes):
    hermes.token = "hermes-held-token"
    stub.search_body = {"query": "q", "results": [RESULT]}
    note = _note(first_use.GrounderWebSearchProvider().search("q"))
    assert "on your Grounder account" in note and "auth add" not in note


def test_a_search_key_says_it_runs_on_the_account(first_use, stub, keyed):
    stub.search_body = {"query": "q", "results": [RESULT]}
    note = _note(first_use.GrounderWebSearchProvider().search("q"))
    assert "search key" in note and "on your Grounder account" in note and "free" not in note


def test_nothing_is_said_where_the_config_cannot_be_written(first_use, stub, free, monkeypatch):
    stub.search_body = {"query": "q", "results": [RESULT]}
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "is_managed", lambda: True)
    provider = first_use.GrounderWebSearchProvider()
    assert _note(provider.search("q")) == "" and _note(provider.search("q")) == ""


def test_a_failed_search_does_not_use_up_the_line(first_use, stub, free, tmp_path):
    stub.search_body = {"query": "q", "results": [RESULT]}
    stub.search_status = 500
    first_use.GrounderWebSearchProvider().search("q")
    assert not (tmp_path / "config.yaml").exists()
