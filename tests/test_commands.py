# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search`` and ``/search_presets``: what they offer, what they save, what a search then sends, and when they stand aside."""

import ast
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
         "offerings": [{"type": "exa", "mode": None}, {"type": "exa:instant", "mode": "instant"}, {"type": "exa:deep", "mode": "deep"}]},
        {"provider": "tavily", "display_name": "Tavily", "offerings": [{"type": "tavily", "mode": None}]},
        # Grounder lists parallel:fast as an offering, but it is Parallel's default search by another name and a request that names it is a 400.
        {"provider": "parallel", "display_name": "Parallel",
         "offerings": [{"type": "parallel", "mode": None}, {"type": "parallel:fast", "mode": "fast"}, {"type": "parallel:turbo", "mode": "turbo"}]}],
    "live_data_apis": [{"id": "polymarket", "display_name": "Polymarket"}, {"id": "espn", "display_name": "ESPN"}]}
PROFILES = {"profiles": [
    {"id": "fast", "name": "Fast", "description": "Quick answers", "includes": ["exa:instant", "tavily"]},
    {"id": "cheap", "name": "Cheap", "description": "Lowest cost"},
    {"id": "live", "name": "Live", "description": "Fresh data"},
    {"id": "deep", "name": "Deep", "description": "Thorough research", "includes": ["exa:deep", "gone:mode"]},
    {"id": "code", "name": "Code", "description": "Software questions"}, {"id": "balanced", "name": "Balanced", "description": "A bit of each"}]}
TELEGRAM_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
# Slack's own commands, which an app cannot register (hermes_cli/commands_platforms.py _SLACK_RESERVED_COMMANDS, Hermes v0.23.1): a command of this name is run on Slack
# as `/hermes <name>`. `search` is one of them, so the docs say so.
SLACK_RESERVED = {"me", "status", "away", "dnd", "shrug", "remind", "msg", "feed", "who", "collapse", "expand", "leave", "join", "open", "search", "topic", "mute", "pro", "shortcuts"}


class Host:
    """The slice of Hermes's plugin context the plugin uses. ``answers`` are what ``ask_choice`` returns, in order (none left: nothing can ask here)."""

    def __init__(self, answers=(), surface="cli", plugin_id="grounder", commands=True, choices=True, manager=None, form=False):
        self.answers, self.surface, self.asked, self.commands, self.providers, self.hooks = list(answers), surface, [], {}, [], {}
        self.plugin_id = plugin_id
        self.pages = None
        if form is not False:  # a Hermes that offers ctx.ask_form: the answers to it ({page id: value}), or None for a form the user cancelled
            self.form_answers = form
            self.supports_form = lambda: True
            self.ask_form = self._ask_form
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
    assert host.commands["search"].args_hint == "[provider|provider:mode|preset|auto|apis]" and host.commands["search_presets"].args_hint == "[preset|auto]"
    assert all("Grounder" in command.description for command in host.commands.values())
    assert [p.name for p in host.providers] == ["grounder"]  # the backend is registered beside them


def test_every_command_name_fits_telegram_and_slacks_reserved_ones_are_documented_with_their_hermes_route(plugin):
    host = Host()
    plugin.register(host)
    assert all(TELEGRAM_NAME.match(name) for name in host.commands)  # Telegram: a-z, 0-9 and _, up to 32
    reserved = sorted(set(host.commands) & SLACK_RESERVED)
    assert reserved == ["search"]  # Slack's own /search: a Slack user reaches this one as `/hermes search`
    docs = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md")}
    manifest_description = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name in reserved:
        assert all(f"/hermes {name}" in text for text in docs.values()), "the docs say how to reach a command Slack reserves"
        assert f"/hermes {name}" in manifest_description
        assert f"/hermes {name}" in host.commands[name].description
    # And the name a Slack user types is not another command's: the registered names do not collide with Slack's other reserved words.
    assert "search_presets" not in SLACK_RESERVED
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
    assert host.commands["search"].handler("exa") == "Grounder will always include Exa in searches."
    assert host.commands["search_presets"].handler("deep") == "Grounder will search with the Deep preset."
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "deep"  # a preset clears a pinned provider


# --- nobody is signed in ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,what", [("search_command", "providers"), ("presets_command", "a preset")])
def test_with_no_key_and_no_sign_in_a_command_says_how_to_sign_in_and_asks_nothing(plugin, hermes, lists, tmp_path, command, what):
    host = Host(["exa"])
    reply = getattr(plugin.commands, command)(host, "")
    assert reply.startswith(f"Sign in to choose {what}: run `{SIGN_IN}`, or set a search key in `GROUNDER_API_KEY` (https://grounder.net/account/keys).")
    assert "free searches" in reply
    assert host.asked == [] and _lists_read(lists) == [] and not (tmp_path / "config.yaml").exists()
    assert getattr(plugin.commands, command)(host, "exa") == reply  # an id typed is no different: nothing is saved


# --- /search: the steps every released Hermes asks in ------------------------------------------------------------------

def test_search_asks_let_grounder_pick_a_preset_a_provider_or_the_live_data_apis_first(plugin, signed_in, tmp_path):
    host = Host(["auto"])
    reply = plugin.commands.search_command(host, "")
    (question, choices), = host.asked
    assert question == "How should Grounder search?"
    assert [c["value"] for c in choices] == ["auto", "presets", "providers", "apis"]
    assert choices[0]["label"] == "Let Grounder pick for you based on your prompt"
    assert reply == "Grounder will pick the providers, modes and any preset for each search from your prompt."
    # Signed in, the lists are read through Hermes, which attaches the token: the plugin adds no credential, and spends no search.
    assert [(c["method"], c["url"]) for c in signed_in.calls] == [("GET", "https://api.grounder.net/v1/providers"), ("GET", "https://api.grounder.net/v1/search-profiles")]
    assert not {name.lower() for call in signed_in.calls for name in call["headers"]} & {"authorization", "cookie"}
    assert {r["path"] for r in signed_in.stub.requests} == {"/v1/providers", "/v1/search-profiles"}
    assert not (tmp_path / "config.yaml").exists() or _saved(tmp_path, "preset") is None


def test_the_preset_step_lists_each_preset_with_what_it_uses_where_grounder_says(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:fast"])
    assert plugin.commands.search_command(host, "") == "Grounder will search with the Fast preset."
    (_, _), (question, choices) = host.asked
    assert question == "Which preset should Grounder search with?"
    assert [c["value"] for c in choices] == ["preset:fast", "preset:cheap", "preset:live", "preset:deep", "preset:code", "preset:balanced"]
    assert choices[0]["description"] == "Quick answers Uses 2 providers and 1 mode."
    assert choices[1]["description"] == "Lowest cost"  # no includes from an API before 0.8.15: nothing is said
    assert choices[3]["description"] == "Thorough research Uses 1 provider and 1 mode."  # an offering Grounder does not list is left out
    assert _saved(tmp_path, "preset") == "fast" and _saved(tmp_path, "server") is None


def test_the_provider_step_then_asks_for_a_mode_and_saves_both(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "mode:exa:instant"])
    assert plugin.commands.search_command(host, "") == "Grounder will always include Exa's instant mode in searches."
    (_, _), (question, providers), (mode_question, modes) = host.asked
    assert question == "Which provider should Grounder always include in searches?"
    assert [c["value"] for c in providers] == ["provider:exa", "provider:tavily", "provider:parallel"]
    assert providers[0]["description"] == "2 modes" and providers[1]["description"] == ""
    assert mode_question == "Which Exa mode?"
    assert [c["value"] for c in modes] == ["provider:exa", "mode:exa:instant", "mode:exa:deep"] and modes[0]["label"] == "Any mode"
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") == "instant" and _saved(tmp_path, "preset") is None


def test_a_default_search_Grounder_names_as_a_mode_is_not_offered_as_one(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:parallel", "provider:parallel"])
    plugin.commands.search_command(host, "")
    assert [c["value"] for c in host.asked[-1][1]] == ["provider:parallel", "mode:parallel:turbo"]  # parallel:fast is Parallel itself
    assert _saved(tmp_path, "server") == "provider:parallel" and _saved(tmp_path, "server_mode") is None


def test_any_mode_keeps_the_provider_and_drops_a_mode_pinned_before(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "deep"})
    plugin.commands.search_command(Host(["providers", "provider:exa", "provider:exa"]), "")
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") is None


def test_a_provider_with_no_modes_is_picked_in_one_step(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:tavily"])
    assert plugin.commands.search_command(host, "") == "Grounder will always include Tavily in searches."
    assert len(host.asked) == 2 and _saved(tmp_path, "server") == "provider:tavily"


def test_the_live_data_step_switches_one_api_at_a_time_and_saves_only_the_ones_off(plugin, signed_in, tmp_path):
    host = Host(["apis", "polymarket", "espn", "polymarket", "done"])
    assert plugin.commands.search_command(host, "") == "Live-data APIs turned off: ESPN."
    assert _saved(tmp_path, "live_data_off") == ["espn"]
    labels = [[c["label"] for c in choices] for _, choices in host.asked[1:]]
    assert labels[0] == ["Done", "✓ on: Polymarket", "✓ on: ESPN"] and labels[1] == ["Done", "✗ off: Polymarket", "✓ on: ESPN"]


def test_cancelling_a_step_saves_nothing_and_lists_what_there_is(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(["providers", "provider:exa"]), "")
    assert reply.startswith("Grounder's presets:") and "  exa (Exa): instant, deep" in reply and "  tavily (Tavily)\n" in reply
    assert _saved(tmp_path, "server") is None


def test_with_a_search_key_the_lists_are_read_with_no_credential_and_never_through_hermes(plugin, keyed, hermes, lists, tmp_path):
    assert plugin.commands.search_command(Host(), "api:polymarket") == "Grounder will always include Polymarket in searches."
    assert plugin.commands.presets_command(Host(["fast"]), "") == "Grounder will search with the Fast preset. It no longer always includes polymarket."  # a preset clears a pin
    assert hermes.calls == [] and _lists_read(lists) == ["/v1/providers", "/v1/search-profiles", "/v1/search-profiles"]
    assert all("Authorization" not in r["headers"] for r in lists.requests)
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "fast"


# --- /search with an argument ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("arg,saved,words", [
    ("exa", {"server": "provider:exa", "server_mode": None, "preset": None}, "Grounder will always include Exa in searches."),
    ("EXA", {"server": "provider:exa", "server_mode": None, "preset": None}, "Grounder will always include Exa in searches."),
    ("provider:tavily", {"server": "provider:tavily", "server_mode": None, "preset": None}, "Grounder will always include Tavily in searches."),
    ("exa:instant", {"server": "provider:exa", "server_mode": "instant", "preset": None}, "Grounder will always include Exa's instant mode in searches."),
    ("mode:exa:deep", {"server": "provider:exa", "server_mode": "deep", "preset": None}, "Grounder will always include Exa's deep mode in searches."),
    ("fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder will search with the Fast preset."),
    ("Fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder will search with the Fast preset."),
    ("preset:deep", {"server": None, "server_mode": None, "preset": "deep"}, "Grounder will search with the Deep preset."),
    ("mode:fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder will search with the Fast preset."),  # what 0.5 called a preset
    ("polymarket", {"server": "api:polymarket", "server_mode": None, "preset": None}, "Grounder will always include Polymarket in searches."),
    ("api:polymarket", {"server": "api:polymarket", "server_mode": None, "preset": None}, "Grounder will always include Polymarket in searches."),
])
def test_search_with_an_argument_skips_the_questions(plugin, signed_in, tmp_path, arg, saved, words):
    host = Host()
    assert plugin.commands.search_command(host, arg) == words
    assert host.asked == [] and {key: _saved(tmp_path, key) for key in saved} == saved


def test_a_provider_pinned_after_a_preset_clears_it_and_a_preset_after_a_provider_clears_that(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"preset": "deep"})
    plugin.commands.search_command(Host(), "exa:instant")
    assert (_saved(tmp_path, "preset"), _saved(tmp_path, "server"), _saved(tmp_path, "server_mode")) == (None, "provider:exa", "instant")
    plugin.commands.search_command(Host(), "cheap")
    assert (_saved(tmp_path, "preset"), _saved(tmp_path, "server"), _saved(tmp_path, "server_mode")) == ("cheap", None, None)


def test_search_auto_clears_the_pick_and_keeps_the_rest(plugin, signed_in, tmp_path):
    _config(tmp_path, search_backend="grounder", grounder={"server": "provider:exa", "server_mode": "deep", "preset": "deep", "live_data_off": ["espn"]})
    assert plugin.commands.search_command(Host(), "auto") == "Grounder will pick the providers, modes and any preset for each search from your prompt."
    assert (_saved(tmp_path, "server"), _saved(tmp_path, "server_mode"), _saved(tmp_path, "preset")) == (None, None, None)
    assert _saved(tmp_path, "live_data_off") == ["espn"]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]["search_backend"] == "grounder"


def test_search_with_an_unknown_argument_or_the_default_search_alias_lists_what_there_is_and_saves_nothing(plugin, signed_in, tmp_path):
    for arg in ("nope", "parallel:fast"):
        reply = plugin.commands.search_command(Host(), arg)
        assert reply.splitlines()[0] == f"Grounder has no preset, provider or mode called {arg}."
        assert "  exa (Exa): instant, deep" in reply and "  polymarket (Polymarket)" in reply and "  fast (Fast): Quick answers" in reply and "`/search auto`" in reply
        assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") is None


def test_an_id_that_is_more_than_one_kind_of_thing_needs_its_prefix(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "x", "display_name": "X search"}], "live_data_apis": [{"id": "x", "display_name": "X posts"}]}
    reply = plugin.commands.search_command(Host(), "x")
    assert reply.splitlines()[0] == "x is more than one thing: use `/search provider:x` or `/search api:x`." and _saved(tmp_path, "server") is None
    assert plugin.commands.search_command(Host(), "api:x") == "Grounder will always include X posts in searches." and _saved(tmp_path, "server") == "api:x"


def test_when_nothing_can_ask_search_lists_what_there_is(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder's presets:\n  fast (Fast): Quick answers\n")
    assert "Grounder's providers, each with its modes:\n  exa (Exa): instant, deep\n  tavily (Tavily)\n  parallel (Parallel): turbo\n" in reply
    assert "Live-data APIs, all on unless turned off:\n  polymarket (Polymarket)\n" in reply and "`/search <id>`" in reply
    assert _saved(tmp_path, "server") is None


# --- a surface that caps a card ----------------------------------------------------------------------------------------

def test_a_provider_card_a_surface_caps_is_asked_in_pages_each_with_a_way_on_and_back(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"P{n}"} for n in range(30)], "live_data_apis": []}
    host = Host(["providers", "__more__", "__back__", "__more__", "provider:p25"], surface="discord")
    assert plugin.commands.search_command(host, "") == "Grounder will always include P25 in searches."
    assert [len(choices) for _, choices in host.asked] == [4, 23, 9, 23, 9]  # 22 providers and More…, then 8 and ← Previous
    pages = [[c["value"] for c in choices] for _, choices in host.asked[1:]]
    assert pages[0][-1] == "__more__" and "__back__" not in pages[0] and pages[1][-1] == "__back__" and "__more__" not in pages[1]
    assert "provider:p22" in pages[1] and _saved(tmp_path, "server") == "provider:p25"
    short = Host(["providers", "provider:exa", "provider:exa"], surface="discord")
    lists.providers_body = PROVIDERS
    plugin.commands.search_command(short, "")
    assert len(short.asked) == 3  # a short list is one card, wherever it is shown


def test_every_page_of_the_live_data_card_a_surface_caps_has_done_and_a_way_back_and_the_card_returns_to_its_page(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa"}], "live_data_apis": [{"id": f"a{n}", "display_name": f"A{n}"} for n in range(25)]}
    # WhatsApp lists take ten: Done, seven APIs, and the ways on and back.
    host = Host(["apis", "__more__", "__more__", "a14", "__back__", "done"], surface="whatsapp_cloud")
    assert plugin.commands.search_command(host, "") == "Live-data APIs turned off: A14."
    cards = [[c["value"] for c in choices] for _, choices in host.asked[1:]]
    assert all(card[0] == "done" and len(card) <= 10 for card in cards)  # Done on every page
    assert [("__back__" in card, "__more__" in card) for card in cards[:3]] == [(False, True), (True, True), (True, True)]
    assert cards[3][1] == "a14"  # after the switch the card is asked again on the page it was on (the third, which starts at a14)
    assert cards[3][0] == "done" and "__back__" in cards[3]
    assert _saved(tmp_path, "live_data_off") == ["a14"]


# --- /search_presets -------------------------------------------------------------------------------------------------------

def test_presets_offers_auto_then_every_preset_the_api_lists(plugin, signed_in, tmp_path):
    host = Host(["deep"])
    reply = plugin.commands.presets_command(host, "")
    (question, choices), = host.asked
    assert question == "Which preset should Grounder search with?"
    assert [c["value"] for c in choices] == ["auto", "fast", "cheap", "live", "deep", "code", "balanced"]
    assert choices[0]["label"] == "Let Grounder pick for you based on your prompt" and choices[4]["label"] == "Deep" and choices[4]["description"] == "Thorough research"
    assert reply == "Grounder will search with the Deep preset." and _saved(tmp_path, "preset") == "deep"
    assert [c["url"] for c in signed_in.calls] == ["https://api.grounder.net/v1/search-profiles"]


@pytest.mark.parametrize("arg", ["deep", "DEEP", "Deep"])
def test_presets_with_an_id_or_a_name_skips_the_question(plugin, signed_in, tmp_path, arg):
    host = Host()
    assert plugin.commands.presets_command(host, arg) == "Grounder will search with the Deep preset."
    assert host.asked == [] and _saved(tmp_path, "preset") == "deep"


def test_presets_auto_clears_the_preset_and_keeps_a_pinned_provider(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "instant", "preset": "deep"})
    assert plugin.commands.presets_command(Host(), "auto") == "Grounder will pick the preset for each search from your prompt."
    assert _saved(tmp_path, "preset") is None and _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") == "instant"


def test_presets_with_an_unknown_preset_lists_the_presets_and_when_nothing_can_ask_lists_them_too(plugin, signed_in, tmp_path):
    reply = plugin.commands.presets_command(Host(), "nope")
    assert reply.splitlines()[0] == "Grounder has no preset called nope."
    assert "  fast (Fast): Quick answers" in reply and "`/search_presets auto`" in reply
    assert plugin.commands.presets_command(Host(), "").startswith("Grounder's presets:\n  fast (Fast): Quick answers")
    assert _saved(tmp_path, "preset") is None


# --- the form, where a Hermes offers ctx.ask_form -----------------------------------------------------------------------

def test_a_hermes_with_ask_form_gets_two_pages_the_presets_in_a_row_the_modes_under_their_provider_and_what_each_preset_lights(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "deep", "live_data_off": ["espn"]})
    host = Host(form={"pick": "preset:fast", "apis": ["polymarket"]})
    plugin.commands.search_command(host, "")
    assert host.asked == []
    pick, apis = host.pages
    assert (pick["id"], pick["tab"], pick["optional"], pick["selected"]) == ("pick", "Search", True, "mode:exa:deep")  # it starts where the saved choice is
    values = [c["value"] for c in pick["choices"] if "value" in c]
    assert values == ["auto", "preset:fast", "preset:cheap", "preset:live", "preset:deep", "preset:code", "preset:balanced",
                      "provider:exa", "mode:exa:instant", "mode:exa:deep", "provider:tavily", "provider:parallel", "mode:parallel:turbo"]
    assert pick["choices"][0]["label"] == "Let Grounder pick for you based on your prompt"
    assert pick["choices"][1] == {"header": "Presets", "row": True} and {"header": "Providers"} in pick["choices"]
    by_value = {c["value"]: c for c in pick["choices"] if "value" in c}
    assert by_value["preset:fast"]["lights"] == ["mode:exa:instant", "provider:tavily"] and by_value["preset:deep"]["lights"] == ["mode:exa:deep"]
    assert "lights" not in by_value["preset:cheap"]  # an API that does not say what a preset includes lights nothing
    assert by_value["mode:exa:instant"]["parent"] == "provider:exa" and "parent" not in by_value["provider:exa"]
    assert "parallel:fast" not in str(pick) and by_value["provider:exa"]["description"].startswith("Embeddings search. Always included in a search")
    assert (apis["id"], apis["tab"], apis["multi_select"], apis["optional"]) == ("apis", "Live-data APIs", True, True)
    assert [c["value"] for c in apis["choices"]] == ["polymarket", "espn"] and apis["checked"] == ["polymarket"]  # ESPN was turned off


def test_what_the_form_answers_is_saved_and_the_live_apis_left_unchecked_are_the_ones_off(plugin, signed_in, tmp_path):
    host = Host(form={"pick": "mode:exa:deep", "apis": ["polymarket"]})
    assert plugin.commands.search_command(host, "") == "Grounder will always include Exa's deep mode in searches. Live-data APIs turned off: ESPN."
    assert (_saved(tmp_path, "server"), _saved(tmp_path, "server_mode"), _saved(tmp_path, "preset"), _saved(tmp_path, "live_data_off")) == ("provider:exa", "deep", None, ["espn"])
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket", "espn"]}), "") == (
        "Grounder will pick the providers, modes and any preset for each search from your prompt. Every live-data API is on.")
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "live_data_off") is None  # nothing off: the key is not kept


def test_a_form_that_changes_only_the_live_data_page_leaves_the_pick_it_cannot_show_alone(plugin, signed_in, tmp_path):
    for saved in ({"server": "api:espn"}, {"server": "provider:defunct", "server_mode": "x"}, {"preset": "gone"}):
        _config(tmp_path, grounder=saved)
        host = Host(form={"pick": "auto", "apis": ["polymarket"]})  # page one answers as it started: on "Let Grounder pick", the nearest row
        assert plugin.commands.search_command(host, "") == "Live-data APIs turned off: ESPN."
        assert {k: _saved(tmp_path, k) for k in saved} == saved and _saved(tmp_path, "live_data_off") == ["espn"]
    _config(tmp_path, grounder={"preset": "fast"})
    assert plugin.commands.search_command(Host(form={"pick": "preset:fast", "apis": ["polymarket", "espn"]}), "") == "Nothing changed."
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket", "espn"]}), "") == (
        "Grounder will pick the providers, modes and any preset for each search from your prompt.")


def test_a_form_of_one_page_has_no_tab_hint(plugin, signed_in, lists, tmp_path):
    only = Host(form={"apis": ["espn", "polymarket"]})
    plugin.commands.search_command(only, "apis")
    assert "Tab" not in only.pages[0]["hint"]
    lists.providers_body = {**PROVIDERS, "live_data_apis": []}
    host = Host(form={"pick": "auto"})
    plugin.commands.search_command(host, "")
    assert [p["id"] for p in host.pages] == ["pick"] and "Tab" not in host.pages[0]["hint"]  # with no live-data APIs listed there is no second page


def test_an_api_that_is_new_to_grounder_is_on_in_the_form_until_the_user_turns_it_off(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_off": ["espn"]})
    lists.providers_body = {**PROVIDERS, "live_data_apis": [*PROVIDERS["live_data_apis"], {"id": "newcomer", "display_name": "Newcomer"}]}
    host = Host(form=None)
    plugin.commands.search_command(host, "")
    assert host.pages[1]["checked"] == ["polymarket", "newcomer"]


def test_a_cancelled_form_changes_nothing_and_search_apis_is_the_second_page_alone(plugin, signed_in, tmp_path):
    assert plugin.commands.search_command(Host(form=None), "") == "Nothing changed." and not (tmp_path / "config.yaml").exists()
    host = Host(form={"apis": ["espn"]})
    assert plugin.commands.search_command(host, "apis") == "Live-data APIs turned off: Polymarket."
    assert [page["id"] for page in host.pages] == ["apis"] and host.pages[0]["optional"] is False
    assert _saved(tmp_path, "live_data_off") == ["polymarket"]


def test_a_hermes_whose_form_is_not_available_here_asks_in_steps(plugin, signed_in, tmp_path):
    host = Host(["auto"], form=None)
    host.supports_form = lambda: False
    assert plugin.commands.search_command(host, "").startswith("Grounder will pick") and host.pages is None and len(host.asked) == 1


def test_a_saved_api_pin_from_an_earlier_version_starts_the_form_on_let_grounder_pick_and_the_next_pick_replaces_it(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "api:polymarket"})
    host = Host(form={"pick": "preset:deep", "apis": ["polymarket", "espn"]})
    assert plugin.commands.search_command(host, "").startswith("Grounder will search with the Deep preset. It no longer always includes Polymarket.")
    assert host.pages[0]["selected"] == "auto" and _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "deep"


# --- the lists come from the API, when asked -------------------------------------------------------------------------------

def test_the_lists_are_read_each_time_so_a_new_provider_or_preset_is_on_offer_at_once(plugin, signed_in, lists):
    plugin.commands.search_command(Host(["auto"]), "")
    lists.providers_body = {"providers": [{"provider": "newcomer", "display_name": "Newcomer"}], "live_data_apis": []}
    lists.profiles_body = {"profiles": [{"id": "later", "name": "Later", "description": "Added since"}]}
    second, third = Host(["providers", "provider:newcomer"]), Host(["later"])
    plugin.commands.search_command(second, "")
    plugin.commands.presets_command(third, "")
    assert [c["value"] for c in second.asked[1][1]] == ["provider:newcomer"]
    assert [c["value"] for c in third.asked[0][1]] == ["auto", "later"]
    assert _lists_read(lists) == ["/v1/providers", "/v1/search-profiles"] * 2 + ["/v1/search-profiles"]


def test_no_provider_preset_or_live_data_id_is_written_into_the_plugin():
    ids = {row["id"] for row in PROFILES["profiles"]}
    tree = ast.parse((PLUGIN_DIR / "commands.py").read_text())
    # The one provider-specific line is the pair of default searches Grounder lists as modes but refuses as one (routing-settings.ts DEFAULT_ALIASES).
    aliases = next(node for node in tree.body if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DEFAULT_ALIASES")
    assert ast.literal_eval(aliases.value) == {"parallel": "fast", "valyu": "all"}
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and not any(node is inner for inner in ast.walk(aliases))}
    assert not strings & ids and not strings & {"exa", "tavily", "polymarket", "espn", "instant", "turbo"}


def test_a_list_that_cannot_be_read_is_said_and_nothing_is_saved(plugin, keyed, lists, tmp_path, monkeypatch):
    lists.list_status = 503
    assert plugin.commands.search_command(Host(["providers"]), "") == "Could not load Grounder's list of providers: HTTP 503"
    assert plugin.commands.presets_command(Host(["fast"]), "") == "Could not load Grounder's list of presets: HTTP 503"
    lists.list_status = 200
    monkeypatch.setattr(plugin.provider, "PROVIDERS_URL", "http://127.0.0.1:9/v1/providers")
    reply = plugin.commands.search_command(Host(["providers"]), "")
    assert reply.startswith("Could not load Grounder's list of providers: Could not reach Grounder")
    assert _saved(tmp_path, "server") is None


def test_a_sign_in_hermes_refuses_is_said_not_raised(plugin, hermes, lists):
    hermes.raises = plugin.provider.AuthError("grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder", code="oauth_client_id_missing")
    assert plugin.commands.search_command(Host(), "") == "Could not load Grounder's list of providers: Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set."


def test_on_slack_the_steps_run_with_no_form_and_every_provider_is_on_one_card(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"P{n}"} for n in range(22)], "live_data_apis": []}
    host = Host(["providers", "provider:p21"], surface="slack")  # `/hermes search`: Slack's own /search is not this command
    assert plugin.commands.search_command(host, "") == "Grounder will always include P21 in searches."
    assert [len(choices) for _, choices in host.asked] == [4, 22]
    assert _saved(tmp_path, "server") == "provider:p21"


# --- what a search then sends ---------------------------------------------------------------------------------------------

def _sent(stub):
    return json.loads([r for r in stub.requests if r["path"] == "/v1/search"][-1]["body"])


def _searches(stub):
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search"]


def test_a_signed_in_search_sends_what_the_user_chose_and_stops_when_it_is_cleared(plugin, signed_in, lists, tmp_path):
    host = Host()
    plugin.commands.search_command(host, "exa:instant")
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800, "routing_settings": {"providers": {"exa": {"use": "always", "modes": {"instant": "always"}}}}}
    assert signed_in.calls[-1]["json"] == _sent(lists)
    plugin.commands.search_command(host, "exa")
    provider.search("q")
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}}  # no fallback setting: the pin is "always included", and the account's own setting stands
    plugin.commands.presets_command(host, "deep")  # a preset clears the pin
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800, "profile": "deep"}
    plugin.commands.search_command(host, "polymarket")
    provider.search("q")
    assert _sent(lists)["routing_settings"] == {"specialized": {"polymarket": {"use": "always"}}} and "profile" not in _sent(lists)
    plugin.commands.search_command(host, "auto")
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_each_live_data_api_turned_off_is_sent_off_and_a_pinned_one_stays_on(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_off": ["espn", "polymarket"], "server": "provider:exa", "server_mode": "deep"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always", "modes": {"deep": "always"}}},
                                                 "specialized": {"espn": {"use": "off"}, "polymarket": {"use": "off"}}}
    _config(tmp_path, grounder={"live_data_off": ["espn", "polymarket"], "server": "api:espn"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"] == {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "off"}}}
    assert "fallback" not in _sent(lists)["routing_settings"]


def test_a_search_with_a_key_sends_them_too(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "fast", "server": "provider:tavily", "live_data_off": ["espn"]})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["profile"] == "fast"
    assert _sent(lists)["routing_settings"] == {"providers": {"tavily": {"use": "always"}}, "specialized": {"espn": {"use": "off"}}}


def test_a_free_search_sends_none_of_what_the_user_chose(plugin, free, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa", "server_mode": "deep", "live_data_off": ["espn"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search_tool({"query": "q"})
    assert len(_searches(lists)) == 2 and all("routing_settings" not in body and "profile" not in body for body in _searches(lists))
    assert all("Authorization" not in r["headers"] for r in lists.requests if r["path"] == "/v1/search")


@pytest.mark.parametrize("grounder,routing", [
    ({"preset": "Not An Id!", "server": "exa", "server_mode": "x"}, None),
    ({"preset": 7, "server": ["provider:exa"], "live_data_off": "espn"}, None),
    ({"preset": "", "server": " ", "live_data_off": [3, "Bad Id!", None]}, None),
    ({"server": "provider:exa", "server_mode": 7}, {"providers": {"exa": {"use": "always"}}}),  # a mode that is not a string is dropped, the pin stays
    ("deep", None), (None, None)])
def test_a_saved_value_of_another_shape_reads_as_unset(plugin, keyed, lists, tmp_path, grounder, routing):
    _config(tmp_path, grounder=grounder)
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800, **({"routing_settings": routing} if routing else {})}


def test_a_config_that_cannot_be_read_changes_nothing_about_a_search(plugin, keyed, lists, monkeypatch):
    import hermes_cli.config as config

    def broken():
        raise OSError("unreadable")

    monkeypatch.setattr(config, "load_config", broken)
    assert plugin.GrounderWebSearchProvider().search("q")["success"]
    assert _sent(lists) == {"query": "q", "top_k": 5, "text_max_chars": 800}


def test_the_user_chose_wins_over_what_the_model_passes_for_the_same_provider_or_api(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa", "live_data_off": ["espn"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q"})
    assert _sent(lists)["profile"] == "deep"
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "specialized": {"espn": {"use": "off"}}}
    # The model chooses its own preset for one search; what it says about other settings is added, and the user's stand where both speak.
    provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"fallback": False}})
    assert _sent(lists)["profile"] == "fast"
    assert _sent(lists)["routing_settings"] == {"fallback": False, "providers": {"exa": {"use": "always"}}, "specialized": {"espn": {"use": "off"}}}
    provider.search_tool({"query": "q", "routing_settings": {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "always"}},
                                                                   "providers": {"exa": {"use": "off"}, "brave": {"use": "always"}}}})
    assert _sent(lists)["routing_settings"] == {"specialized": {"espn": {"use": "off"}, "polymarket": {"use": "always"}},
                                                 "providers": {"exa": {"use": "always"}, "brave": {"use": "always"}}}


def test_a_page_read_sends_none_of_the_users_choices(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa", "live_data_off": ["espn"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", "urls": ["https://example.com/a"]})
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)
    provider.search(" ".join(["https://example.com/a", "more"]))
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)
    provider.extract(["https://example.com/a"])
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)


def test_the_tool_says_the_users_choices_win_and_names_a_preset(plugin):
    properties = plugin.provider.SEARCH_SCHEMA["parameters"]["properties"]
    assert "wins over what you pass" in properties["routing_settings"]["description"]
    schema = properties["profile"]
    assert schema["type"] == "string" and "grounder_list_providers" in schema["description"] and "search_modes" in schema["description"] and "preset" in schema["description"]


# --- a choice Grounder no longer offers ------------------------------------------------------------------------------------

def _refused(message):
    return 400, {"error": {"code": "invalid_request", "message": message}}


_OK = (200, {"query": "q", "results": [{"title": "T", "url": "https://example.com/a", "text": "x", "source": "exa"}]})


@pytest.mark.parametrize("saved,message,left,told", [
    ({"preset": "turbo"}, 'No such search profile: turbo. GET /v1/search-profiles lists them; "auto" is none.', {"preset": None}, "the preset `turbo`"),
    ({"preset": "turbo"}, "No such search profile: turbo.", {"preset": None}, "the preset `turbo`"),
    ({"server": "provider:exa", "server_mode": "bogusmode"}, "providers.exa.modes: no mode bogusmode; its modes are default, instant, deep",
     {"server": "provider:exa", "server_mode": None}, "the `bogusmode` mode of exa you always included"),
    ({"server": "provider:defunct", "server_mode": "x"}, "No such provider: defunct", {"server": None, "server_mode": None}, "the provider `defunct` you always included"),
    ({"server": "api:gone-api"}, "No such specialized API: gone-api", {"server": None}, "the live-data API `gone-api` you always included"),
    ({"live_data_off": ["gone-api", "espn"]}, "No such specialized API: gone-api", {"live_data_off": ["espn"]}, "the live-data API `gone-api` you turned off"),
])
def test_a_400_that_names_a_saved_choice_grounder_no_longer_offers_drops_it_tells_the_user_and_searches_once_more_without_it(plugin, signed_in, lists, tmp_path, saved, message, left, told):
    _config(tmp_path, grounder=saved)
    lists.search_script = [_refused(message), _OK]
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"] and len(result["data"]["web"]) == 1
    assert told in result["data"]["notice"] and "Tell the user this" in result["data"]["notice"] and "no longer offers" in result["data"]["notice"]
    first, second = _searches(lists)
    assert len(_searches(lists)) == 2  # the same search, sent again without the choice
    ident = told.split("`")[1]
    assert ident in json.dumps(first) and ident not in json.dumps(second)
    assert first["query"] == second["query"] == "q"
    for key, value in left.items():
        assert _saved(tmp_path, key) == value
    for key in set(saved) - set(left):
        assert _saved(tmp_path, key) == saved[key]  # nothing else saved was touched


def test_two_stale_choices_are_dropped_one_400_at_a_time_and_both_are_told(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "turbo", "server": "provider:defunct"})
    lists.search_script = [_refused("No such search profile: turbo. GET /v1/search-profiles lists them."), _refused("No such provider: defunct"), _OK]
    result = plugin.GrounderWebSearchProvider().search_tool({"query": "q"})
    data = json.loads(result)["data"]
    assert "the preset `turbo` or the provider `defunct` you always included" in data["notice"] and len(_searches(lists)) == 3
    assert _saved(tmp_path, "preset") is None and _saved(tmp_path, "server") is None
    assert "profile" not in _sent(lists) and "routing_settings" not in _sent(lists)


def test_a_400_for_what_the_model_passed_or_for_nothing_saved_is_not_the_users_to_drop(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "turbo", "server": "provider:exa"})
    lists.search_script = [_refused("No such search profile: turbo. GET /v1/search-profiles lists them.")]
    result = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q", "profile": "turbo"}))
    assert result["success"] is False and "No such search profile: turbo" in result["error"] and len(_searches(lists)) == 1
    assert _saved(tmp_path, "preset") == "turbo"
    lists.search_script = [_refused("No such provider: defunct")]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False and _saved(tmp_path, "server") == "provider:exa"  # not the saved choice
    lists.search_script = [_refused("Unknown setting: whatever")]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False and _saved(tmp_path, "preset") == "turbo"
    lists.search_script = [(503, {"error": {"code": "unavailable", "message": "later"}})]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False and len(_searches(lists)) == 4


def test_a_400_that_keeps_naming_choices_stops_after_the_most_one_search_may_drop(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_off": [f"gone{n}" for n in range(6)]})
    lists.search_script = [_refused(f"No such specialized API: gone{n}") for n in range(6)]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False
    assert len(_searches(lists)) == plugin.provider.MAX_FORGET + 1 and _saved(tmp_path, "live_data_off") == ["gone4", "gone5"]


def test_a_free_search_has_no_choice_to_drop(plugin, free, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "turbo"})
    lists.search_script = [_refused("No such search profile: turbo.")]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False and len(_searches(lists)) == 1 and _saved(tmp_path, "preset") == "turbo"


def test_a_config_that_cannot_be_written_leaves_the_choice_and_says_the_400(plugin, signed_in, lists, tmp_path, monkeypatch):
    _config(tmp_path, grounder={"preset": "turbo"})
    lists.search_script = [_refused("No such search profile: turbo.")]
    monkeypatch.setattr(plugin.provider, "save_settings", lambda **changes: (_ for _ in ()).throw(OSError("read-only")))
    result = plugin.GrounderWebSearchProvider().search("q")
    assert result["success"] is False and "No such search profile: turbo" in result["error"] and _saved(tmp_path, "preset") == "turbo"
