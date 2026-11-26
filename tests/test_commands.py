# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search``, the plugin's one command: the hub it opens, what it saves, what a search then sends, and when it is registered at all."""

import json
import logging
import re
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_DIR, yaml

SIGN_IN = "hermes auth add grounder:grounder"
PROVIDERS = {
    "providers": [
        {"provider": "exa", "display_name": "Exa", "description": "Embeddings search.",
         "offerings": [{"type": "exa", "mode": None}, {"type": "exa:instant", "mode": "instant", "features": ["web-search", "page-text"]},
                       {"type": "exa:deep", "mode": "deep", "limits": "A research pass over many pages; it takes longer.", "features": ["deep-research"]}]},
        {"provider": "tavily", "display_name": "Tavily", "offerings": [{"type": "tavily", "mode": None}]},
        # Grounder lists parallel:fast as an offering, but it is Parallel's default search by another name and a request that names it is a 400.
        {"provider": "parallel", "display_name": "Parallel",
         "offerings": [{"type": "parallel", "mode": None}, {"type": "parallel:fast", "mode": "fast"}, {"type": "parallel:turbo", "mode": "turbo"}]}],
    "live_data_apis": [{"id": "polymarket", "display_name": "Polymarket"}, {"id": "espn", "display_name": "ESPN"}]}
TELEGRAM_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
# Slack's own commands, which an app cannot register (hermes_cli/commands_platforms.py _SLACK_RESERVED_COMMANDS, Hermes v0.23.1): a command of this name is run on Slack
# as `/hermes <name>`. `search` is one of them, so the docs say so.
SLACK_RESERVED = {"me", "status", "away", "dnd", "shrug", "remind", "msg", "feed", "who", "collapse", "expand", "leave", "join", "open", "search", "topic", "mute", "pro", "shortcuts"}


class Host:
    """The slice of Hermes's plugin context the plugin uses. ``answers`` are what ``ask_choice`` returns, in order (none left: nothing can ask here)."""

    def __init__(self, answers=(), surface="cli", plugin_id="grounder", commands=True, choices=True, manager=None, form=False, selected=False):
        self.answers, self.surface, self.asked, self.commands, self.providers, self.hooks = list(answers), surface, [], {}, [], {}
        self.plugin_id = plugin_id
        self.pages = None
        self.tools = []
        if form is not False:  # a Hermes that offers ctx.ask_form: the answers to it ({page id: value}), or None for a form the user cancelled
            self.form_answers = form
            self.supports_form = lambda: True
            self.ask_form = self._ask_form
        # ``selected``: this profile has been set up before (the plugin's mark is set), so the first-load selection does not run
        self.state = SimpleNamespace(get=lambda key, default=None: True if selected and key == "backends_selected" else default, set=lambda key, value: None)
        if manager is not None:
            self._manager = manager
        if commands:
            self.register_command = self._register_command
        if choices:
            self.ask_choice = self._ask_choice

    def register_web_search_provider(self, provider):
        self.providers.append(provider)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, name, **kwargs):
        self.tools.append(name)

    def _register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = SimpleNamespace(handler=handler, description=description, args_hint=args_hint)

    def _ask_form(self, pages):
        self.pages = pages
        return self.form_answers

    def _ask_choice(self, question, choices):
        self.asked.append((question, choices))
        answer = self.answers.pop(0) if self.answers else None
        assert answer is None or answer in [c["value"] for c in choices], f"{answer!r} is not on this card"
        return answer

    def session_surface(self):
        return self.surface


def _puts(stub):
    """The bodies the plugin wrote to the account's setting, in order."""
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search/settings" and r["method"] == "PUT"]


def _config(home, **web):
    (home / "config.yaml").write_text(yaml.safe_dump({"web": web}))


@pytest.fixture
def lists(stub):
    stub.providers_body = PROVIDERS
    return stub


@pytest.fixture
def signed_in(hermes, lists):
    hermes.token = "hermes-held-token"
    return hermes


# --- registering, and standing aside -------------------------------------------------------------------------------------

def test_register_adds_search_and_only_search_with_its_hint(plugin):
    host = Host()
    plugin.register(host)
    assert set(host.commands) == {"search", "search-direct-format", "search-login", "search-logout"}  # the presets are a choice in /search's first picker, not a command
    assert host.commands["search"].args_hint == "[provider|direct|direct off|format|resolver]"
    assert host.commands["search"].description.startswith("Choose Grounder provider, Grounder direct")
    assert [p.name for p in host.providers] == ["grounder"]  # the backend is registered beside it


def test_every_command_name_fits_telegram_and_slacks_reserved_ones_are_documented_with_their_hermes_route(plugin):
    host = Host()
    plugin.register(host)
    assert all(TELEGRAM_NAME.match(name.replace("-", "_")) for name in host.commands)  # Hermes sends search-direct-format to Telegram as search_format  # Telegram: a-z, 0-9 and _, up to 32
    reserved = sorted(set(host.commands) & SLACK_RESERVED)
    assert reserved == ["search"]  # Slack's own /search: a Slack user reaches this one as `/hermes search`
    docs = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md")}
    manifest_description = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name in reserved:
        assert all(f"/hermes {name}" in text for text in docs.values()), "the docs say how to reach a command Slack reserves"
        assert f"/hermes {name}" in manifest_description
        assert f"/hermes {name}" in host.commands[name].description
    try:  # where Hermes itself is installed, the pinned set is checked against its own
        from hermes_cli.commands_platforms import _SLACK_RESERVED_COMMANDS
    except ImportError:
        return
    assert set(host.commands) & set(_SLACK_RESERVED_COMMANDS) == set(reserved)


@pytest.mark.parametrize("missing", ["commands", "choices"])
def test_a_hermes_that_cannot_register_a_command_or_ask_a_choice_gets_none_and_search_works(plugin, missing):
    host = Host(**{missing: False})
    plugin.register(host)
    assert host.commands == {}
    assert [p.name for p in host.providers] == ["grounder"] and "transform_tool_result" in host.hooks


def test_a_hermes_that_bundles_grounders_provider_keeps_its_commands(plugin, monkeypatch, caplog):
    monkeypatch.setattr(plugin, "find_spec", lambda name: SimpleNamespace(name=name) if name == "plugins.web.grounder" else None)
    host = Host()
    with caplog.at_level(logging.INFO):
        plugin.register(host)
    assert host.commands == {} and [p.name for p in host.providers] == ["grounder"]
    assert any("already has /search" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("answer,registers", [(ImportError("no plugins package"), True), (ValueError("no spec"), False), (None, True)])
def test_the_bundled_check_reads_the_import_system(plugin, monkeypatch, answer, registers):
    def find_spec(name):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(plugin, "find_spec", find_spec)
    host = Host()
    plugin.register(host)
    assert bool(host.commands) is registers


def test_another_plugins_commands_are_left_alone_and_this_plugins_own_are_replaced(plugin):
    held = {"search": {"plugin_key": "web-grounder"}}
    other = Host(manager=SimpleNamespace(_plugin_commands=held))
    plugin.register(other)
    assert other.commands == {}
    mine = Host(manager=SimpleNamespace(_plugin_commands={"search": {"plugin_key": "grounder"}}))
    plugin.register(mine)
    assert set(mine.commands) == {"search", "search-direct-format", "search-login", "search-logout"}


def test_the_registered_handlers_run_the_commands(plugin, free):
    host = Host(["only"])
    plugin.register(host)
    assert "Grounder direct" in host.commands["search"].handler("")
    assert plugin.direct_dev.state() == "only"


@pytest.mark.parametrize("web,registers", [
    ({"search_backend": "grounder"}, True), ({"search_backend": "Grounder"}, True), ({"backend": "grounder"}, True),  # the older key, where search_backend is not set
    ({"search_backend": "firecrawl"}, False), ({"search_backend": "firecrawl", "backend": "grounder"}, False),  # search_backend wins
    ({"backend": "exa"}, False), ({}, False), ({"search_backend": None}, False), ({"search_backend": ""}, False)])
def test_search_is_registered_only_while_the_profiles_web_search_backend_is_grounder(plugin, tmp_path, web, registers):
    _config(tmp_path, **web)
    host = Host(selected=True)  # a profile that has been set up: nothing here selects Grounder
    plugin.register(host)
    assert set(host.commands) == ({"search", "search-direct-format", "search-login", "search-logout"} if registers else set())
    # With another backend the model tools and the web provider are registered as ever: only the command goes.
    assert host.tools == ["grounder_search"] and [p.name for p in host.providers] == ["grounder"]
    assert "transform_tool_result" in host.hooks and "pre_llm_call" in host.hooks


def test_a_profile_with_no_config_at_all_registers_no_command(plugin, tmp_path):
    host = Host(selected=True)
    plugin.register(host)
    assert host.commands == {} and not (tmp_path / "config.yaml").exists()


def test_the_first_load_with_another_backend_chosen_leaves_it_and_registers_no_command(plugin, tmp_path):
    _config(tmp_path, search_backend="firecrawl", extract_backend="firecrawl")
    host = Host()  # the first load in this profile: a backend the user chose is left alone, so Grounder is not the backend and /search is not there
    plugin.register(host)
    assert host.commands == {}
    web = yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]
    assert web == {"search_backend": "firecrawl", "extract_backend": "firecrawl"}


def test_the_load_that_makes_grounder_the_backend_registers_the_command_in_the_same_load(plugin, tmp_path):
    _config(tmp_path, keyless_fallback=False)  # no backend chosen yet
    host = Host()  # the first load in this profile: the plugin selects itself, then registers
    plugin.register(host)
    assert set(host.commands) == {"search", "search-direct-format", "search-login", "search-logout"}
    web = yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]
    assert web["search_backend"] == "grounder" and web["extract_backend"] == "grounder"
    second = Host(selected=True)  # the next start: the backend is Grounder, so the command is there again
    plugin.register(second)
    assert set(second.commands) == {"search", "search-direct-format", "search-login", "search-logout"}


def test_a_switch_made_in_hermes_tools_takes_effect_at_the_next_start_and_back_again(plugin, tmp_path):
    first = Host()
    plugin.register(first)
    assert set(first.commands) == {"search", "search-direct-format", "search-login", "search-logout"}
    _config(tmp_path, search_backend="tavily", extract_backend="tavily")  # the user picks another backend in `hermes tools`
    plugin.register(Host(selected=True))
    again = Host(selected=True)
    plugin.register(again)
    assert again.commands == {}
    _config(tmp_path, search_backend="grounder")
    back = Host(selected=True)
    plugin.register(back)
    assert set(back.commands) == {"search", "search-direct-format", "search-login", "search-logout"}


def test_a_managed_install_is_left_alone_and_registers_the_command_only_where_the_config_names_grounder(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "is_managed", lambda: True)
    _config(tmp_path, search_backend="firecrawl")
    host = Host()
    plugin.register(host)
    assert host.commands == {} and yaml.safe_load((tmp_path / "config.yaml").read_text())["web"] == {"search_backend": "firecrawl"}
    _config(tmp_path, search_backend="grounder")
    named = Host()
    plugin.register(named)
    assert set(named.commands) == {"search", "search-direct-format", "search-login", "search-logout"}


def test_a_config_that_cannot_be_read_registers_no_command_and_takes_nothing_else_down(plugin, tmp_path, monkeypatch, caplog):
    def broken():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr(plugin, "load_config", broken)
    host = Host(selected=True)
    with caplog.at_level(logging.WARNING):
        plugin.register(host)
    assert host.commands == {} and [p.name for p in host.providers] == ["grounder"] and host.tools == ["grounder_search"]
    assert any("Could not read the web backend" in record.getMessage() for record in caplog.records)


# --- nobody is signed in ------------------------------------------------------------------------------------------------

def test_search_menu_marks_the_saved_choice_and_writes_it_once(plugin, hermes, stub):
    hermes.token = "signed-in"
    stub.saved_direct = {"use": "off", "format": "summary"}
    plugin.provider._direct_cache.update(at=0, value=None)
    host = Host(["only"])
    result = plugin.commands.search_command(host, "")
    assert "Search with: Grounder direct" in result
    assert plugin.direct_dev.SEARCH_PRICE in result
    assert [item["value"] for item in host.asked[0][1]] == ["off", "only", "format", "resolver", "done"]
    assert host.asked[0][1][0]["label"].endswith("(current)")
    saves = [json.loads(r["body"]) for r in stub.requests if r["method"] == "PUT"]
    assert saves == [{"search": {"mode": "grounder-direct"}}]
    assert stub.saved_direct["use"] == "only"

    assert "Search with: Grounder provider" in plugin.commands.search_command(Host(), "provider")
    assert "Highlights" in plugin.commands.search_command(Host(), "format highlights")
    assert plugin.provider.save_direct({"user_agent": "Test Safari/17.0"}) is True
    assert _puts(stub) == [
        {"search": {"mode": "grounder-direct"}},
        {"search": {"mode": "grounder-provider"}},
        {"direct": {"format": "highlights"}},
        {"direct": {"user_agent": "Test Safari/17.0"}},
    ]
    assert stub.saved_direct == {"use": "off", "format": "highlights", "user_agent": "Test Safari/17.0"}


def test_search_format_and_resolver_offer_the_current_choice(plugin, free):
    host = Host(["format", "highlights"])
    assert "Highlights" in plugin.commands.search_command(host, "")
    assert host.asked[1][1][0]["label"] == "Summary (current)"
    host = Host(["resolver", "grounder"])
    assert "Grounder's resolver" in plugin.commands.search_command(host, "")
    assert plugin.direct_dev.resolver() == "grounder"


def test_a_free_caller_can_change_the_local_mode(plugin, free):
    host = Host()
    assert "Grounder direct" in plugin.commands.search_command(host, "direct")
    assert plugin.direct_dev.state() == "only"
    assert "Grounder provider" in plugin.commands.search_command(host, "direct off")
    assert plugin.direct_dev.state() == "off"
    assert not host.asked
