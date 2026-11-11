# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The product is Grounder, with an uppercase G, in every sentence a person or an agent reads (Harlan, 2026-11-11).

A Hermes agent that read "the grounder plugin" and "switch web.search_backend to grounder" repeated both to its user. Lowercase stays only where the word
is an identifier, and prose that shows an identifier puts it in backticks: the plugin and provider id `grounder`, a config key (`web.search_backend`),
an env name, a command (`hermes auth add grounder:grounder`), a repo or an address (`grounder-labs/grounder-hermes-plugin`, `api.grounder.net`).
These tests read the manifest, both Markdown files, every string the plugin's code holds and the messages it renders, and fail on a lowercase
"grounder" used as a word.
"""

import ast
import re
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_DIR, manifest

FENCED = re.compile(r"```.*?```", re.S)
CODE_SPAN = re.compile(r"`[^`\n]*`")
ADDRESS = re.compile(r"https?://\S+|\]\([^)\s]*\)")
# The word alone: not inside an identifier, a path, an address, a command or a dashed name (those have a . / : _ - @ $ or a word character beside it).
WORD = re.compile(r"(?<![\w./:@$-])grounder(?![\w/:@-])(?!\.\w)")


def lowercase_words(text):
    """The lowercase "grounder"s in ``text`` that stand as a word, once code, fenced blocks, addresses and link targets are set aside."""
    plain = ADDRESS.sub(" ", CODE_SPAN.sub(" ", FENCED.sub(" ", text)))
    return [plain[max(0, m.start() - 25):m.end() + 25].replace("\n", " ") for m in WORD.finditer(plain)]


def test_the_check_flags_the_word_and_passes_identifiers_in_backticks_and_addresses():
    for sentence in ("Install the grounder plugin.", "Switch web.search_backend to grounder.", "grounder is installed", "Ask grounder, then wait.", "It is grounder's job.", "(grounder)"):
        assert lowercase_words(sentence), sentence
    for sentence in ("Grounder is installed.", "Run `hermes auth add grounder:grounder`.", "Enable it with `hermes plugins enable grounder`.",
                     "See https://github.com/grounder-labs/grounder-hermes-plugin and api.grounder.net.", "The `grounder` toolset and `web.grounder.server`.",
                     "[repo](https://github.com/grounder-labs/grounder)", "```sh\nhermes auth add grounder:grounder\n```", "Set `grounder_search` on.",
                     "From grounder.net, or grounder-labs/grounder-hermes-plugin, or $HERMES_HOME/grounder, or grounder_search."):
        assert lowercase_words(sentence) == [], sentence


def _description_values(node):
    """Every value under a ``description`` key of the manifest."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _description_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _description_values(item)


def test_the_manifests_words_say_Grounder():
    values = list(_description_values(manifest()))
    assert len(values) >= 2
    for value in values:
        assert lowercase_words(value) == [], value


@pytest.mark.parametrize("name", ["README.md", "after-install.md"])
def test_the_documents_say_Grounder(name):
    assert lowercase_words((PLUGIN_DIR / name).read_text()) == []


def _strings(path):
    """The string literals of a source file that are prose: with a space in them, and not a docstring (a docstring is for the developer reading the code)."""
    tree = ast.parse(path.read_text())
    docstrings = {id(body[0].value) for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  for body in [node.body] if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)}
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings and " " in node.value]


@pytest.mark.parametrize("name", ["provider.py", "commands.py", "__init__.py"])
def test_every_sentence_the_code_holds_says_Grounder(name):
    strings = _strings(PLUGIN_DIR / name)
    assert strings
    for text in strings:
        assert lowercase_words(text) == [], text


def _all_text(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_text(item)


def test_what_the_plugin_renders_says_Grounder(plugin):
    provider = plugin.GrounderWebSearchProvider()
    texts = [*_all_text(plugin.provider.SEARCH_SCHEMA), *_all_text(plugin.provider.LIST_PROVIDERS_SCHEMA), provider.get_setup_schema()["tag"],
             *plugin.provider.UNREAD.values(), plugin.provider.NOT_READ, plugin.provider.FILES_NOTE, plugin.provider.SHOWN_NOTE,
             plugin.provider.CITE_REQUEST, plugin.provider.CITE_REQUEST_LISTED, plugin.commands.sign_in_first("servers"), plugin.commands.sign_in_first("modes")]
    links = {"signup_url": "https://grounder.net/account", "billing_url": "https://grounder.net/account/billing"}
    for status, via, body in ((401, "key", {}), (401, "sign-in", {}), (429, "free", {"error": {"details": links}}), (503, "free", {"error": {"details": links}}),
                              (429, "key", {"error": {"message": "slow down"}}), (500, "sign-in", {})):
        texts.append(plugin.provider._error_message(status, body, via))
    host = SimpleNamespace(commands={}, register_command=lambda name, handler, description="", args_hint="": host.commands.update({name: description}))
    plugin.commands.register_commands(host)
    texts += host.commands.values()
    for text in texts:
        assert lowercase_words(text) == [], text
