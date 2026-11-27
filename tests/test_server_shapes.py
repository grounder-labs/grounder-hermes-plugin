"""The plugin's fake server against what Grounder's server really answers (GRO-1154).

api_shapes.json ``settings`` and ``search`` are recorded from the server by workers/router/test/surface-parity.test.ts, in memory, never by hand. These tests replay
each recorded exchange against conftest's Stub and feed the recorded search to the plugin, so a change on either side fails here or there until the other follows.
"""
import json
from pathlib import Path

import pytest
from conftest import SETTINGS, Stub

SHAPES = json.loads((Path(__file__).parent / "api_shapes.json").read_text())


def test_a_fresh_account_reads_the_settings_the_server_answers():
    assert Stub().settings("GET", None) == (200, SETTINGS["answer"])


@pytest.mark.parametrize("exchange", SETTINGS["exchanges"], ids=lambda exchange: json.dumps(exchange["body"]))
def test_the_fake_server_answers_each_settings_change_as_the_server_does(exchange):
    status, answer = Stub().settings("PUT", json.dumps(exchange["body"]))
    assert status == exchange["status"]
    if status == 200:
        assert {key: answer[key] for key in exchange["answer"]} == exchange["answer"]
    else:
        assert answer["error"]["message"] == exchange["message"]


def test_the_plugin_reads_the_search_the_server_answers(plugin, stub, free):
    recorded = SHAPES["search"]
    stub.search_body = recorded["answer"]
    found = plugin.GrounderWebSearchProvider().search(recorded["query"])
    assert found["success"]
    rows = found["data"]["web"]
    assert [(row["url"], row["title"]) for row in rows] == [(row["url"], row["title"]) for row in recorded["answer"]["results"]]
