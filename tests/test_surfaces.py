# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The plugin touches only what Hermes publishes, and reads clean to Hermes's own scans (GRO-866).

``hermes plugins validate`` warned of three core internals at 0.7.2 (``tools.url_safety.normalize_url_for_request``, ``agent.redact._PREFIX_RE`` and
``tools.web_result_cache.search_memo``) and read one line of the plugin as an inline shell snippet. These tests hold both: the imports against Hermes's published plugin API,
the key families the plugin refuses against Hermes's own list, and every file against the scan's pattern.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
HERMES_TOP = {"agent", "hermes_cli", "tools", "gateway", "hermes_constants", "hermes_state", "utils", "plugins", "cron", "providers"}
# What the plugin may import of Hermes: hermes_cli/data/plugin_api.json's published entries it uses (a module, or ``module:Name``).
PUBLISHED = {"agent.secret_scope", "agent.web_search_provider", "hermes_cli.auth_constants:AuthError", "hermes_cli.plugin_provider_requests", "hermes_cli.config",
             "gateway.session_context:get_session_env", "tools.url_safety:is_safe_url"}


def _imports(path):
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found += [(node.module, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [(alias.name, "") for alias in node.names]
    return found


def test_the_plugin_imports_only_what_hermes_publishes():
    private = []
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        for module, name in _imports(path):
            if module.split(".")[0] in HERMES_TOP and module not in PUBLISHED and f"{module}:{name}" not in PUBLISHED:
                private.append(f"{path.name}: {module}.{name}".rstrip("."))
    assert private == []


def _sample(prefix, length=40):
    return prefix + "A" * length  # built here, so no file of the plugin holds a key-shaped line


# One address a family: its prefix and a body long enough for Hermes's own floor of that family.
FAMILIES = {"sk-": 40, "ctx7sk-": 20, "ghp_": 20, "github_pat_": 20, "gho_": 20, "ghu_": 20, "ghs_": 20, "ghr_": 20, "xoxb-": 20, "xapp-1-": 20, "AIza": 40, "pplx-": 20, "fal_": 20,
            "fc-": 20, "bb_live_": 20, "gAAAA": 30, "AKIA": 16, "sk_live_": 20, "sk_test_": 20, "rk_live_": 20, "SG.": 20, "hf_": 20, "r8_": 20, "npm_": 20, "pypi-": 20, "dop_v1_": 20,
            "doo_v1_": 20, "am_": 30, "sk_": 20, "tvly-": 20, "exa_": 20, "gsk_": 20, "syt_": 20, "retaindb_": 20, "hsk-": 20, "mem0_": 20, "brv_": 20, "xai-": 40, "ntn_": 20, "fw-": 40,
            "fw_": 40, "fpk_": 40, "glpat-": 20, "gloas-": 20, "gldt-": 20, "glrt-": 20, "glrtr-": 20, "glcbt-": 20, "glptt-": 20, "glft-": 20, "glimt-": 20, "glagent-": 20,
            "glsoat-": 20, "glffct-": 20, "glwt-": 20, "GR1348941": 20, "pk-lf-": 20}


@pytest.mark.parametrize("prefix", sorted(FAMILIES))
def test_an_address_carrying_a_key_of_each_family_hermes_redacts_is_refused(plugin, prefix):
    key = _sample(prefix, FAMILIES[prefix])
    refusal = plugin.provider._refusal
    assert refusal(f"https://example.com/page?k={key}") == plugin.provider.SECRET_REFUSED
    assert refusal(f"https://example.com/{key}/read") == plugin.provider.SECRET_REFUSED


def test_a_percent_encoded_key_is_refused_too(plugin):
    key = _sample("sk-", 30).replace("-", "%2D")
    assert plugin.provider._refusal(f"https://example.com/?k={key}") == plugin.provider.SECRET_REFUSED


def test_grounders_own_key_and_the_key_in_use_are_refused(plugin, monkeypatch):
    assert plugin.provider._refusal(f"https://example.com/?k={_sample('gr-', 30)}") == plugin.provider.SECRET_REFUSED
    monkeypatch.setattr(plugin.provider, "_key", lambda: "a-search-key-in-use-1234")
    assert plugin.provider._refusal("https://example.com/?k=a-search-key-in-use-1234") == plugin.provider.SECRET_REFUSED


@pytest.mark.parametrize("address", ["https://example.com/blog/sk-learn", "https://example.com/scikit-learn-1.5", "https://example.com/a/ghp_notes", "https://example.com/?ref=fc-12",
                                      "https://example.com/talks/exa_intro", "https://en.wikipedia.org/wiki/Python_(programming_language)"])
def test_an_ordinary_address_that_only_resembles_a_key_prefix_is_not_refused(plugin, address):
    assert plugin.provider._refusal(address) in ("", plugin.provider.PRIVATE_REFUSED) and plugin.provider._refusal(address) != plugin.provider.SECRET_REFUSED


def test_the_vendored_key_list_covers_every_family_this_hermes_redacts(plugin):
    redact = sys.modules.get("agent.redact")
    try:
        import agent.redact as redact  # noqa: F811  a test may read Hermes's internals; the plugin does not
    except ImportError:
        pytest.skip("this environment has no Hermes to compare with")
    theirs = getattr(redact, "_PREFIX_PATTERNS", None)
    if not theirs:
        pytest.skip("this Hermes keeps its key list another way")
    assert [pattern for pattern in theirs if pattern not in plugin.provider.KEY_PATTERNS] == []


def test_an_address_check_needs_no_normalizer(plugin, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.url_safety", None)
    refusal = plugin.provider._refusal
    assert refusal("https://example.com/Köln/straße") == "" and refusal("http://localhost:8080/") == plugin.provider.PRIVATE_REFUSED
    assert refusal(f"https://example.com/K%C3%B6ln?k={_sample('ghp_', 20)}") == plugin.provider.SECRET_REFUSED


# Hermes's skill scan (tools/skills_guard.py, ``inline_shell_exec``) reads a bang and a backtick followed by text and a closing backtick as a snippet to run. Spelled here from the
# character, so this file holds no such line itself.
TICK = chr(96)
INLINE_SHELL = re.compile("!" + TICK + "[^" + TICK + r"\s][^" + TICK + r"\n]*" + TICK)


def test_no_file_of_the_plugin_reads_as_an_inline_shell_snippet_to_hermes_scan():
    hits = []
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md", ".yaml", ".yml", ".json", ".txt") and "__pycache__" not in path.parts:
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if INLINE_SHELL.search(line):
                    hits.append(f"{path.relative_to(PLUGIN_DIR)}:{number}")
    assert hits == []


def test_the_citation_code_pattern_still_finds_fenced_blocks_and_inline_spans(plugin):
    code = plugin.provider.CODE
    fence = TICK * 3
    text = f"keep [1] {fence}\nskip [2]\n{fence} and {TICK}items[3]{TICK} then [4]"
    assert [m.group(0).count("[") for m in code.finditer(text)] == [1, 1]  # the fenced block and the inline span, each holding one bracketed number
    assert "[1]" not in "".join(m.group(0) for m in code.finditer(text)) and "[4]" not in "".join(m.group(0) for m in code.finditer(text))
