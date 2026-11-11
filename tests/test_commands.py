# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search`` and ``/search_presets``: what they offer, what they save, what a search then sends, and when they stand aside."""

import ast
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_DIR, yaml

SIGN_IN = "hermes auth add grounder:grounder"
PROVIDERS = {
    "providers": [{"provider": "exa", "display_name": "Exa"}, {"provider": "tavily", "display_name": "Tavily"}],
    "live_data_apis": [{"id": "polymarket", "display_name": "Polymarket"}]}
PROFILES = {"profiles": [
    {"id": "fast", "name": "Fast", "description": "Quick answers"}, {"id": "cheap", "name": "Cheap", "description": "Lowest cost"},
    {"id": "live", "name": "Live", "description": "Fresh data"}, {"id": "deep", "name": "Deep", "description": "Thorough research"},
    {"id": "code", "name": "Code", "description": "Software questions"}, {"id": "balanced", "name": "Balanced", "description": "A bit of each"}]}


class Host:
    """The slice of Hermes's plugin context the plugin uses. ``answers`` are what ``ask_choice`` returns, in order (none left: nothing can ask here)."""

    def __init__(self, answers=(), surface="cli", plugin_id="grounder", commands=True, choices=True, manager=None):
        self.answers, self.surface, self.asked, self.commands, self.providers, self.hooks = list(answers), surface, [], {}, [], {}
        self.plugin_id = plugin_id
        self.state = SimpleNamespace(get=lambda key, default=None: default, set=lambda key, value: None)
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

    def _register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = SimpleNamespace(handler=handler, description=description, args_hint=args_hint)

    def _ask_choice(self, question, choices):
        self.asked.append((question, choices))
        answer = self.answers.pop(0) if self.answers else None
        assert answer is None or answer in [c["value"] for c in choices], f"{answer!r} is not on this card"
        return answer

    def session_surface(self):
        return self.surface


def _saved(home, key):
    path = home / "config.yaml"
    web = (yaml.safe_load(path.read_text()) or {}).get("web") if path.exists() else None
    return ((web or {}).get("grounder") or {}).get(key)


def _config(home, **web):
    (home / "config.yaml").write_text(yaml.safe_dump({"web": web}))


@pytest.fixture
def lists(stub):
    stub.providers_body, stub.profiles_body = PROVIDERS, PROFILES
    return stub


@pytest.fixture
def signed_in(hermes, lists):
    hermes.token = "hermes-held-token"
    return hermes


def _lists_read(stub):
    return [r["path"] for r in stub.requests if r["path"] in ("/v1/providers", "/v1/search-profiles")]


# --- registering, and standing aside -------------------------------------------------------------------------------------

def test_register_adds_both_commands_with_their_hints(plugin):
    host = Host()
    plugin.register(host)
    assert set(host.commands) == {"search", "search_presets"}
    assert host.commands["search"].args_hint == "[server-id|auto]" and host.commands["search_presets"].args_hint == "[mode-id|auto]"
    assert all("Grounder" in command.description for command in host.commands.values())
    assert [p.name for p in host.providers] == ["grounder"]  # the backend is registered beside them


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
    assert any("already has /search and /search_presets" in record.getMessage() for record in caplog.records)


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
    mine = Host(manager=SimpleNamespace(_plugin_commands={"search": {"plugin_key": "grounder"}, "search_presets": {"plugin_key": "grounder"}}))
    plugin.register(mine)
    assert set(mine.commands) == {"search", "search_presets"}


def test_the_registered_handlers_run_the_commands(plugin, signed_in, tmp_path):
    host = Host()
    plugin.register(host)
    assert host.commands["search"].handler("exa") == "Grounder will always use Exa for searches."
    assert host.commands["search_presets"].handler("deep") == "Grounder will search in Deep mode."
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "preset") == "deep"


# --- nobody is signed in ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,what", [("search_command", "servers"), ("presets_command", "modes")])
def test_with_no_key_and_no_sign_in_a_command_says_how_to_sign_in_and_asks_nothing(plugin, hermes, lists, tmp_path, command, what):
    host = Host(["exa"])
    reply = getattr(plugin.commands, command)(host, "")
    assert reply.startswith(f"Sign in to choose {what}: run `{SIGN_IN}`, or set a search key in `GROUNDER_API_KEY` (https://grounder.net/account/keys).")
    assert "free searches" in reply
    assert host.asked == [] and _lists_read(lists) == [] and not (tmp_path / "config.yaml").exists()
    assert getattr(plugin.commands, command)(host, "exa") == reply  # an id typed is no different: nothing is saved


# --- /search --------------------------------------------------------------------------------------------------------------

def test_search_offers_auto_then_every_provider_and_live_api_the_api_lists(plugin, signed_in, tmp_path):
    host = Host(["provider:exa"])
    reply = plugin.commands.search_command(host, "")
    (question, choices), = host.asked
    assert question == "Which server should Grounder always use for searches?"
    assert [c["value"] for c in choices] == ["auto", "provider:exa", "provider:tavily", "api:polymarket"]
    assert choices[0]["label"] == "Let Grounder pick" and choices[1]["label"] == "Exa" and choices[3]["description"] == "live-data API"
    assert reply == "Grounder will always use Exa for searches." and _saved(tmp_path, "server") == "provider:exa"
    # Signed in, the list is read through Hermes, which attaches the token: the plugin adds no credential, and spends no search.
    call = signed_in.calls[0]
    assert (call["method"], call["url"]) == ("GET", "https://api.grounder.net/v1/providers")
    assert not {name.lower() for name in call["headers"]} & {"authorization", "cookie"}
    assert [r["path"] for r in signed_in.stub.requests] == ["/v1/providers"]


def test_with_a_search_key_the_lists_are_read_with_no_credential_and_never_through_hermes(plugin, keyed, hermes, lists, tmp_path):
    assert plugin.commands.search_command(Host(["api:polymarket"]), "") == "Grounder will always use Polymarket for searches."
    assert plugin.commands.presets_command(Host(["fast"]), "") == "Grounder will search in Fast mode."
    assert hermes.calls == [] and _lists_read(lists) == ["/v1/providers", "/v1/search-profiles"]
    assert all("Authorization" not in r["headers"] for r in lists.requests)
    assert _saved(tmp_path, "server") == "api:polymarket" and _saved(tmp_path, "preset") == "fast"


@pytest.mark.parametrize("arg,value,name", [("exa", "provider:exa", "Exa"), ("EXA", "provider:exa", "Exa"), ("provider:tavily", "provider:tavily", "Tavily"),
                                            ("polymarket", "api:polymarket", "Polymarket"), ("api:polymarket", "api:polymarket", "Polymarket")])
def test_search_with_an_id_skips_the_question(plugin, signed_in, tmp_path, arg, value, name):
    host = Host()
    assert plugin.commands.search_command(host, arg) == f"Grounder will always use {name} for searches."
    assert host.asked == [] and _saved(tmp_path, "server") == value


def test_search_auto_removes_the_server_and_keeps_the_rest(plugin, signed_in, tmp_path):
    _config(tmp_path, search_backend="grounder", grounder={"server": "provider:exa", "preset": "deep"})
    assert plugin.commands.search_command(Host(), "auto") == "Grounder picks the providers for each search."
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "deep"
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]["search_backend"] == "grounder"
    _config(tmp_path, grounder={"server": "provider:exa"})
    assert plugin.commands.search_command(Host(["auto"]), "") == "Grounder picks the providers for each search."  # the question's first choice
    assert _saved(tmp_path, "server") is None


def test_search_with_an_unknown_id_lists_what_there_is_and_saves_nothing(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "nope")
    assert reply.splitlines()[0] == "Grounder has no server called nope."
    assert "  exa (Exa)" in reply and "  polymarket (Polymarket)" in reply and "`/search auto`" in reply
    assert _saved(tmp_path, "server") is None


def test_an_id_that_is_both_a_provider_and_a_live_api_needs_its_prefix(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "x", "display_name": "X search"}], "live_data_apis": [{"id": "x", "display_name": "X posts"}]}
    reply = plugin.commands.search_command(Host(), "x")
    assert "both a search provider and a live-data API" in reply and _saved(tmp_path, "server") is None
    assert plugin.commands.search_command(Host(), "api:x") == "Grounder will always use X posts for searches." and _saved(tmp_path, "server") == "api:x"


def test_when_nothing_can_ask_search_lists_the_servers(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder's search providers:\n  exa (Exa)\n  tavily (Tavily)\nLive-data APIs:\n  polymarket (Polymarket)\n")
    assert "`/search <id>`" in reply and _saved(tmp_path, "server") is None


def test_a_card_that_a_surface_caps_is_asked_in_two_steps(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"P{n}"} for n in range(30)],
                            "live_data_apis": [{"id": f"a{n}", "display_name": f"A{n}"} for n in range(5)]}
    host = Host(["providers", "__more__", "provider:p25"], surface="discord")
    assert plugin.commands.search_command(host, "") == "Grounder will always use P25 for searches."
    assert [len(choices) for _, choices in host.asked] == [3, 24, 7]
    assert host.asked[0][1][1]["label"] == "Search providers"
    assert host.asked[1][1][-1]["label"] == "More…" and "provider:p22" in [c["value"] for c in host.asked[1][1]]
    assert _saved(tmp_path, "server") == "provider:p25"
    short = Host(["provider:exa"], surface="discord")
    lists.providers_body = PROVIDERS
    plugin.commands.search_command(short, "")
    assert len(short.asked) == 1  # a short list is one card, wherever it is shown


# --- /search_presets -------------------------------------------------------------------------------------------------------

def test_presets_offers_auto_then_every_mode_the_api_lists(plugin, signed_in, tmp_path):
    host = Host(["deep"])
    reply = plugin.commands.presets_command(host, "")
    (question, choices), = host.asked
    assert question == "Which mode should Grounder search in?"
    assert [c["value"] for c in choices] == ["auto", "fast", "cheap", "live", "deep", "code", "balanced"]
    assert choices[0]["label"] == "Let Grounder pick" and choices[4]["label"] == "Deep" and choices[4]["description"] == "Thorough research"
    assert reply == "Grounder will search in Deep mode." and _saved(tmp_path, "preset") == "deep"
    assert signed_in.calls[0]["url"] == "https://api.grounder.net/v1/search-profiles"


@pytest.mark.parametrize("arg", ["deep", "DEEP", "Deep"])
def test_presets_with_an_id_or_a_name_skips_the_question(plugin, signed_in, tmp_path, arg):
    host = Host()
    assert plugin.commands.presets_command(host, arg) == "Grounder will search in Deep mode."
    assert host.asked == [] and _saved(tmp_path, "preset") == "deep"


def test_presets_auto_removes_the_mode_and_keeps_the_server(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa", "preset": "deep"})
    assert plugin.commands.presets_command(Host(), "auto") == "Grounder picks the mode for each search."
    assert _saved(tmp_path, "preset") is None and _saved(tmp_path, "server") == "provider:exa"


def test_presets_with_an_unknown_mode_lists_the_modes_and_when_nothing_can_ask_lists_them_too(plugin, signed_in, tmp_path):
    reply = plugin.commands.presets_command(Host(), "nope")
    assert reply.splitlines()[0] == "Grounder has no mode called nope."
    assert "  fast (Fast): Quick answers" in reply and "`/search_presets auto`" in reply
    assert plugin.commands.presets_command(Host(), "").startswith("Grounder's modes:\n  fast (Fast): Quick answers")
    assert _saved(tmp_path, "preset") is None


# --- the lists come from the API, when asked -------------------------------------------------------------------------------

def test_the_lists_are_read_each_time_so_a_new_server_or_mode_is_on_offer_at_once(plugin, signed_in, lists):
    first = Host(["auto"])
    plugin.commands.search_command(first, "")
    lists.providers_body = {"providers": [{"provider": "newcomer", "display_name": "Newcomer"}], "live_data_apis": []}
    lists.profiles_body = {"profiles": [{"id": "later", "name": "Later", "description": "Added since"}]}
    second, third = Host(["auto"]), Host(["auto"])
    plugin.commands.search_command(second, "")
    plugin.commands.presets_command(third, "")
    assert [c["value"] for c in second.asked[0][1]] == ["auto", "provider:newcomer"]
    assert [c["value"] for c in third.asked[0][1]] == ["auto", "later"]
    assert _lists_read(lists) == ["/v1/providers", "/v1/providers", "/v1/search-profiles"]


def test_no_server_or_mode_is_written_into_the_plugin():
    modes = {row["id"] for row in PROFILES["profiles"]}
    strings = {node.value for node in ast.walk(ast.parse((PLUGIN_DIR / "commands.py").read_text())) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not strings & modes and not strings & {"exa", "tavily", "polymarket"}


def test_a_list_that_cannot_be_read_is_said_and_nothing_is_saved(plugin, keyed, lists, tmp_path, monkeypatch):
    lists.list_status = 503
    assert plugin.commands.search_command(Host(["provider:exa"]), "") == "Could not load Grounder's list of servers: HTTP 503"
    assert plugin.commands.presets_command(Host(["fast"]), "") == "Could not load Grounder's list of modes: HTTP 503"
    lists.list_status = 200
    monkeypatch.setattr(plugin.provider, "PROVIDERS_URL", "http://127.0.0.1:9/v1/providers")
    reply = plugin.commands.search_command(Host(["provider:exa"]), "")
    assert reply.startswith("Could not load Grounder's list of servers: Could not reach Grounder")
    assert _saved(tmp_path, "server") is None


def test_a_sign_in_hermes_refuses_is_said_not_raised(plugin, hermes, lists):
    hermes.raises = plugin.provider.AuthError("grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder", code="oauth_client_id_missing")
    assert plugin.commands.search_command(Host(), "") == "Could not load Grounder's list of servers: Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set."


# --- what a search then sends ---------------------------------------------------------------------------------------------

def _sent(stub):
    return json.loads([r for r in stub.requests if r["path"] == "/v1/search"][-1]["body"])


def test_a_signed_in_search_sends_the_chosen_mode_and_server_and_stops_when_they_are_cleared(plugin, signed_in, lists, tmp_path):
    host = Host()
    plugin.commands.search_command(host, "exa")
    plugin.commands.presets_command(host, "deep")
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800, "profile": "deep", "routing_settings": {"providers": {"exa": {"use": "always"}}}}
    assert signed_in.calls[-1]["json"] == _sent(lists)
    plugin.commands.search_command(host, "polymarket")
    provider.search("q")
    assert _sent(lists)["routing_settings"] == {"specialized": {"polymarket": {"use": "always"}}}
    plugin.commands.search_command(host, "auto")
    plugin.commands.presets_command(host, "auto")
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_a_search_with_a_key_sends_them_too(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "fast", "server": "provider:tavily"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["profile"] == "fast" and _sent(lists)["routing_settings"] == {"providers": {"tavily": {"use": "always"}}}


@pytest.mark.parametrize("grounder", [{"preset": "Not An Id!", "server": "exa"}, {"preset": 7, "server": ["provider:exa"]}, {"preset": "", "server": " "}, "deep", None])
def test_a_saved_value_of_another_shape_reads_as_unset(plugin, keyed, lists, tmp_path, grounder):
    _config(tmp_path, grounder=grounder)
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_a_config_that_cannot_be_read_changes_nothing_about_a_search(plugin, keyed, lists, monkeypatch):
    import hermes_cli.config as config

    def broken():
        raise OSError("unreadable")

    monkeypatch.setattr(config, "load_config", broken)
    assert plugin.GrounderWebSearchProvider().search("q")["success"]
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_grounder_search_sends_the_choice_unless_the_model_chose_its_own_and_a_page_read_sends_none(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa"})
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q"})
    assert _sent(lists)["profile"] == "deep" and _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}}
    provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"fallback": False}})
    assert _sent(lists)["profile"] == "fast" and _sent(lists)["routing_settings"] == {"fallback": False}
    provider.search_tool({"query": "q", "urls": ["https://example.com/a"]})
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)
    provider.search(" ".join(["https://example.com/a", "more"]))
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)
    provider.extract(["https://example.com/a"])
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)


def test_grounder_search_takes_a_profile_and_says_where_the_modes_are_listed(plugin):
    schema = plugin.provider.SEARCH_SCHEMA["parameters"]["properties"]["profile"]
    assert schema["type"] == "string" and "grounder_list_providers" in schema["description"] and "search_modes" in schema["description"]
