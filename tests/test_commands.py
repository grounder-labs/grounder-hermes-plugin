# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search``, the plugin's one command: the hub it opens, what it saves, what a search then sends, and when it is registered at all."""

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
         "offerings": [{"type": "exa", "mode": None}, {"type": "exa:instant", "mode": "instant", "features": ["web-search", "page-text"]},
                       {"type": "exa:deep", "mode": "deep", "limits": "A research pass over many pages; it takes longer.", "features": ["deep-research"]}]},
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

def test_register_adds_search_and_only_search_with_its_hint(plugin):
    host = Host()
    plugin.register(host)
    assert set(host.commands) == {"search"}  # the presets are a choice in /search's first picker, not a command
    assert host.commands["search"].args_hint == "[optimized|preset|provider|provider:mode|apis]"
    assert host.commands["search"].description == ("Choose how Grounder searches: Grounder optimized, a Grounder preset, or Grounder direct (one provider and its mode), "
                                                   "and set each live-data API to Every call, Never or Grounder decides (on Slack: /hermes search)")
    assert [p.name for p in host.providers] == ["grounder"]  # the backend is registered beside it


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
    assert set(mine.commands) == {"search"}


def test_the_registered_handlers_run_the_commands(plugin, signed_in, tmp_path):
    host = Host()
    plugin.register(host)
    assert host.commands["search"].handler("exa") == "Grounder direct: Grounder will use only Exa as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."
    assert host.commands["search"].handler("deep") == "Grounder preset Deep: Grounder will pick the best provider for it on each search."
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "deep"  # a preset clears a pinned provider


# --- /search is registered only while Grounder is the web search backend -------------------------------------------------

@pytest.mark.parametrize("web,registers", [
    ({"search_backend": "grounder"}, True), ({"search_backend": "Grounder"}, True), ({"backend": "grounder"}, True),  # the older key, where search_backend is not set
    ({"search_backend": "firecrawl"}, False), ({"search_backend": "firecrawl", "backend": "grounder"}, False),  # search_backend wins
    ({"backend": "exa"}, False), ({}, False), ({"search_backend": None}, False), ({"search_backend": ""}, False)])
def test_search_is_registered_only_while_the_profiles_web_search_backend_is_grounder(plugin, tmp_path, web, registers):
    _config(tmp_path, **web)
    host = Host(selected=True)  # a profile that has been set up: nothing here selects Grounder
    plugin.register(host)
    assert set(host.commands) == ({"search"} if registers else set())
    # With another backend the model tools and the web provider are registered as ever: only the command goes.
    assert host.tools == ["grounder_search", "grounder_list_providers"] and [p.name for p in host.providers] == ["grounder"]
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
    assert set(host.commands) == {"search"}
    web = yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]
    assert web["search_backend"] == "grounder" and web["extract_backend"] == "grounder"
    second = Host(selected=True)  # the next start: the backend is Grounder, so the command is there again
    plugin.register(second)
    assert set(second.commands) == {"search"}


def test_a_switch_made_in_hermes_tools_takes_effect_at_the_next_start_and_back_again(plugin, tmp_path):
    first = Host()
    plugin.register(first)
    assert set(first.commands) == {"search"}
    _config(tmp_path, search_backend="tavily", extract_backend="tavily")  # the user picks another backend in `hermes tools`
    plugin.register(Host(selected=True))
    again = Host(selected=True)
    plugin.register(again)
    assert again.commands == {}
    _config(tmp_path, search_backend="grounder")
    back = Host(selected=True)
    plugin.register(back)
    assert set(back.commands) == {"search"}


def test_a_managed_install_is_left_alone_and_registers_the_command_only_where_the_config_names_grounder(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "is_managed", lambda: True)
    _config(tmp_path, search_backend="firecrawl")
    host = Host()
    plugin.register(host)
    assert host.commands == {} and yaml.safe_load((tmp_path / "config.yaml").read_text())["web"] == {"search_backend": "firecrawl"}
    _config(tmp_path, search_backend="grounder")
    named = Host()
    plugin.register(named)
    assert set(named.commands) == {"search"}


def test_a_config_that_cannot_be_read_registers_no_command_and_takes_nothing_else_down(plugin, tmp_path, monkeypatch, caplog):
    def broken():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr(plugin, "load_config", broken)
    host = Host(selected=True)
    with caplog.at_level(logging.WARNING):
        plugin.register(host)
    assert host.commands == {} and [p.name for p in host.providers] == ["grounder"] and host.tools == ["grounder_search", "grounder_list_providers"]
    assert any("Could not read the web backend" in record.getMessage() for record in caplog.records)


# --- nobody is signed in ------------------------------------------------------------------------------------------------

def test_with_no_key_and_no_sign_in_search_says_how_to_sign_in_and_asks_nothing(plugin, hermes, lists, tmp_path):
    host = Host(["exa"])
    reply = plugin.commands.search_command(host, "")
    assert reply.startswith(f"Sign in to choose providers: run `{SIGN_IN}`, or set a search key in `GROUNDER_API_KEY` (https://grounder.net/account/keys).")
    assert reply.endswith("Until then Grounder searches with Grounder optimized, on its free searches.")
    assert host.asked == [] and _lists_read(lists) == [] and not (tmp_path / "config.yaml").exists()
    assert plugin.commands.search_command(host, "exa") == reply  # an id typed is no different: nothing is saved


# --- /search: the hub every released Hermes asks in -----------------------------------------------------------------------

def _labels(choices):
    return [c["label"] for c in choices]


def _values(choices):
    return [c["value"] for c in choices]


def test_search_opens_the_hub_with_the_current_choice_marked_and_says_nothing_changed_when_nothing_did(plugin, signed_in, tmp_path):
    host = Host(["done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."  # no summary line, for no change
    (question, choices), = host.asked
    assert question == "How should Grounder search?"
    assert _values(choices) == ["auto", "presets", "providers", "apis", "done"]
    assert _labels(choices) == ["Let Grounder pick (current)", "A preset", "A provider", "Live-data APIs", "Done"]
    assert [c.get("description") for c in choices] == ["Grounder optimized: Grounder picks the provider for each question", "Fast, Cheap, Live, Deep, Code, Balanced",
                                                       "Exa, Tavily and Parallel, each with its modes", "2 on Grounder decides", None]
    # Signed in, the lists are read through Hermes, which attaches the token: the plugin adds no credential, and spends no search.
    assert [(c["method"], c["url"]) for c in signed_in.calls] == [("GET", "https://api.grounder.net/v1/providers"), ("GET", "https://api.grounder.net/v1/search-profiles")]
    assert not {name.lower() for call in signed_in.calls for name in call["headers"]} & {"authorization", "cookie"}
    assert {r["path"] for r in signed_in.stub.requests} == {"/v1/providers", "/v1/search-profiles"}
    assert not (tmp_path / "config.yaml").exists()


def test_a_hub_with_no_live_data_apis_has_no_row_for_them(plugin, signed_in, lists):
    lists.providers_body = {"providers": PROVIDERS["providers"], "live_data_apis": []}
    host = Host(["done"])
    plugin.commands.search_command(host, "")
    assert _values(host.asked[0][1]) == ["auto", "presets", "providers", "done"]


def test_a_preset_is_picked_on_its_own_screen_and_the_hub_comes_back_with_it_marked_current(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:fast", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: preset Fast"  # Done answers with one line of what is set now
    (_, hub), (question, presets), (_, hub_after), = host.asked[:3]
    assert question == "Which Grounder preset?"
    assert _values(presets) == ["preset:fast", "preset:cheap", "preset:live", "preset:deep", "preset:code", "preset:balanced", "back"]
    assert _labels(presets)[-1] == "← Back" and _labels(presets)[:2] == ["Fast", "Cheap"]
    assert [c["description"] for c in presets[:4]] == ["Quick answers · 2 providers", "Lowest cost", "Fresh data", "Thorough research · 1 provider"]  # no includes: the purpose stands alone
    assert _labels(hub) == ["Let Grounder pick (current)", "A preset", "A provider", "Live-data APIs", "Done"]
    assert _labels(hub_after) == ["Let Grounder pick", "A preset: Fast (current)", "A provider", "Live-data APIs", "Done"]
    assert _saved(tmp_path, "preset") == "fast" and _saved(tmp_path, "server") is None


def test_the_preset_screen_marks_the_current_preset_and_back_returns_to_the_hub_with_nothing_saved(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"preset": "deep"})
    host = Host(["presets", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert _labels(host.asked[1][1])[3] == "Deep (current)" and _labels(host.asked[0][1])[:2] == ["Let Grounder pick", "A preset: Deep (current)"]
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which Grounder preset?", "How should Grounder search?"]
    assert _saved(tmp_path, "preset") == "deep"


def test_a_preset_row_says_how_many_providers_it_uses_from_the_same_data_and_never_names_them(plugin):
    modes = lambda n: [{"id": f"p{n}:{mode}", "mode": mode, "description": ""} for mode in ("x", "y")]
    catalog = plugin.commands.Catalog([], [{"id": f"p{n}", "name": f"P{n}", "description": "", "modes": modes(n)} for n in range(8)], [])
    row = lambda includes, description="Quickest: one call. More words.": plugin.commands._preset_row({"description": description, "includes": includes}, catalog)
    assert row(["p0", "p1:x", "p2", "p3", "p4:x", "p0:y", "gone"]) == "Quickest · 5 providers"  # a provider counts once however many modes name it; an offering Grounder does not list is left out
    assert row(["p0", "p1"]) == "Quickest · 2 providers" and row(["p3"]) == "Quickest · 1 provider" and row(None) == "Quickest" and row(["gone:x"]) == "Quickest"
    assert row(["p0"], "") == "1 provider" and "P0" not in row(["p0", "p1", "p2", "p3"])
    for text, purpose in (("The quickest answer: one provider call.", "The quickest answer"), ("Pages from the last week; a score is answered live.", "Pages from the last week"),
                          ("One call to a lower-priced provider.", "One call to a lower-priced provider"), ("", "")):
        assert plugin.commands._purpose(text) == purpose


def test_a_provider_is_picked_then_its_mode_and_the_hub_comes_back_with_both_marked(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "mode:exa:instant", "done"])
    reply = plugin.commands.search_command(host, "")
    (_, _), (question, providers), (mode_question, modes), (_, hub_after) = host.asked
    assert question == "Which provider should Grounder direct search with?"
    assert _values(providers) == ["provider:exa", "provider:tavily", "provider:parallel", "back"]
    assert _labels(providers) == ["Exa", "Tavily", "Parallel", "← Back"]  # a provider's name typed picks it
    assert [c["description"] for c in providers[:3]] == ["2 modes", "", "1 mode"]
    assert mode_question == "Which Exa mode?"
    assert _values(modes) == ["provider:exa", "mode:exa:instant", "mode:exa:deep", "back"]
    assert _labels(modes) == ["Default", "instant", "deep", "← Back"]
    assert [c["description"] for c in modes[:3]] == ["Embeddings search", "Web search, page text", "A research pass over many pages"]
    assert _labels(hub_after)[2] == "A provider: Exa instant (current)" and _labels(hub_after)[:2] == ["Let Grounder pick", "A preset"]
    assert reply == "Grounder: direct Exa instant"
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") == "instant" and _saved(tmp_path, "preset") is None


def test_a_mode_screens_back_returns_to_the_providers_and_that_screens_back_to_the_hub(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "back", "provider:tavily", "providers", "back", "done"])
    plugin.commands.search_command(host, "")
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder direct search with?", "Which Exa mode?",
                                                        "Which provider should Grounder direct search with?", "How should Grounder search?",
                                                        "Which provider should Grounder direct search with?", "How should Grounder search?"]
    assert _saved(tmp_path, "server") == "provider:tavily" and _saved(tmp_path, "server_mode") is None  # a provider with no modes is picked in one step


def test_the_mode_screen_marks_default_or_the_mode_that_is_current_and_a_default_search_named_as_a_mode_is_not_offered(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "deep"})
    host = Host(["providers", "provider:exa", "provider:exa", "done"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[2][1]) == ["Default", "instant", "deep (current)", "← Back"]
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") is None  # Default keeps the provider and drops a mode pinned before
    _config(tmp_path, grounder={"server": "provider:exa"})
    host = Host(["providers", "provider:exa", "back", "back", "done"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[2][1])[0] == "Default (current)"
    host = Host(["providers", "provider:parallel", "provider:parallel", "done"])
    plugin.commands.search_command(host, "")
    assert _values(host.asked[2][1]) == ["provider:parallel", "mode:parallel:turbo", "back"]  # parallel:fast is Parallel itself
    assert _saved(tmp_path, "server") == "provider:parallel" and _saved(tmp_path, "server_mode") is None


def test_the_live_apis_screen_sets_all_or_one_api_and_each_change_is_saved_at_once(plugin, signed_in, tmp_path):
    host = Host(["apis", "polymarket", "off", "espn", "always", "back", "done"])
    reply = plugin.commands.search_command(host, "")
    questions = [question for question, _ in host.asked]
    assert questions == ["How should Grounder search?", "Live-data APIs: pick one to change how Grounder uses it.", "How should Grounder use Polymarket?",
                         "Live-data APIs: pick one to change how Grounder uses it.", "How should Grounder use ESPN?", "Live-data APIs: pick one to change how Grounder uses it.",
                         "How should Grounder search?"]
    first = host.asked[1][1]
    assert _values(first) == ["__all__", "polymarket", "espn", "back"] and _labels(first) == ["Set all to Grounder decides", "Polymarket", "ESPN", "← Back"]
    assert [c.get("description") for c in first[1:3]] == ["Grounder decides", "Grounder decides"]
    one = host.asked[2][1]
    assert _values(one) == ["always", "auto", "off", "back"] and _labels(one) == ["Every call", "Grounder decides (current)", "Never", "← Back"]
    assert one[0]["description"] == "Grounder asks Polymarket on every search, in every mode, beside the search provider."
    assert [c.get("description") for c in host.asked[3][1][1:3]] == ["Never", "Grounder decides"]  # the list comes back with the new setting on its row
    assert _labels(host.asked[4][1])[0] == "Every call" and _saved(tmp_path, "live_data_off") == ["polymarket"] and _saved(tmp_path, "live_data_always") == ["espn"]
    hub_after = host.asked[-1][1]
    assert hub_after[3] == {"value": "apis", "label": "Live-data APIs", "description": "1 on Every call, 1 on Never"}  # a setting nothing is on is left out
    assert reply == "Grounder: optimized · Live-data APIs: Polymarket never, ESPN every call"


def test_set_all_returns_every_api_to_grounder_decides_and_the_hub_counts_them(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"live_data_off": ["polymarket"], "live_data_always": ["espn"]})
    host = Host(["apis", "__all__", "back", "done"])
    reply = plugin.commands.search_command(host, "")
    assert [c.get("description") for c in host.asked[1][1][1:3]] == ["Never", "Every call"] and [c.get("description") for c in host.asked[2][1][1:3]] == ["Grounder decides"] * 2
    assert host.asked[0][1][3]["description"] == "1 on Every call, 1 on Never" and host.asked[3][1][3]["description"] == "2 on Grounder decides"
    assert _saved(tmp_path, "live_data_off") is None and _saved(tmp_path, "live_data_always") is None
    assert reply == "Grounder: optimized"  # the APIs are back on Grounder decides, which the line leaves out


def test_done_answers_with_one_line_of_what_is_set_now_and_only_when_something_changed(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:deep", "apis", "espn", "always", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: preset Deep · Live-data APIs: ESPN every call"
    host = Host(["providers", "provider:tavily", "apis", "espn", "auto", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: direct Tavily"  # ESPN is Grounder decides again: left out
    host = Host(["apis", "espn", "always", "espn", "auto", "back", "done"])  # changed, and changed back: nothing is different from when it opened
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    _config(tmp_path, grounder={})
    host = Host(["auto", "done"])  # the pick is already what is set
    assert plugin.commands.search_command(host, "") == "Nothing changed."


def test_the_summary_is_the_commands_reply_and_never_posted_through_post_message(plugin, signed_in, tmp_path):
    # Released Hermes's classic CLI runs the command in its gateway, where ctx.post_message answers ok and shows nothing: only a reply is seen on every surface.
    host = Host(["presets", "preset:code", "done"])
    host.post_message = lambda text: (_ for _ in ()).throw(AssertionError("the summary is a reply, not a post"))
    assert plugin.commands.search_command(host, "") == "Grounder: preset Code"


def test_leaving_the_hub_counts_as_done_and_cancelling_a_screen_returns_to_the_hub(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:fast", None])  # the hub is left, not answered
    assert plugin.commands.search_command(host, "") == "Grounder: preset Fast"
    host = Host(["providers", None, "apis", None, "presets", None, "done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert [question for question, _ in host.asked].count("How should Grounder search?") == 4  # every screen, cancelled, is back at the hub


def test_when_nothing_can_ask_the_hub_gives_way_to_the_listing_and_saves_nothing(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder optimized (`/search optimized`, the default): Grounder picks the provider for your question.\nGrounder preset (`/search <preset>`):\n")
    assert "  exa (Exa): instant, deep" in reply and "  tavily (Tavily)\n" in reply
    assert not (tmp_path / "config.yaml").exists()


def test_search_apis_opens_the_live_apis_screen_alone_and_ends_on_done_with_the_settings_in_words(plugin, signed_in, tmp_path):
    host = Host(["polymarket", "off", "espn", "always", "back"])
    assert plugin.commands.search_command(host, "apis") == "Live-data APIs, in every mode. Every call: ESPN. Never: Polymarket. All the others: Grounder decides."
    assert [question for question, _ in host.asked].count("How should Grounder search?") == 0  # typed, it skips the hub
    assert _labels(host.asked[0][1])[-1] == "Done" and _saved(tmp_path, "live_data_off") == ["polymarket"] and _saved(tmp_path, "live_data_always") == ["espn"]


def test_with_a_search_key_the_lists_are_read_with_no_credential_and_never_through_hermes(plugin, keyed, hermes, lists, tmp_path):
    assert plugin.commands.search_command(Host(), "api:polymarket") == "Live-data APIs, in every mode. Every call: Polymarket. All the others: Grounder decides."
    assert plugin.commands.search_command(Host(), "fast") == "Grounder preset Fast: Grounder will pick the best provider for it on each search."  # a preset leaves the live-data APIs alone
    assert hermes.calls == [] and _lists_read(lists) == ["/v1/providers", "/v1/search-profiles"] * 2
    assert all("Authorization" not in r["headers"] for r in lists.requests)
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "fast"


# --- /search with an argument ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("arg,saved,words", [
    ("exa", {"server": "provider:exa", "server_mode": None, "preset": None}, "Grounder direct: Grounder will use only Exa as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."),
    ("EXA", {"server": "provider:exa", "server_mode": None, "preset": None}, "Grounder direct: Grounder will use only Exa as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."),
    ("provider:tavily", {"server": "provider:tavily", "server_mode": None, "preset": None}, "Grounder direct: Grounder will use only Tavily as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."),
    ("exa:instant", {"server": "provider:exa", "server_mode": "instant", "preset": None}, "Grounder direct: Grounder will use only Exa's instant mode as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."),
    ("mode:exa:deep", {"server": "provider:exa", "server_mode": "deep", "preset": None}, "Grounder direct: Grounder will use only Exa's deep mode as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."),
    ("fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder preset Fast: Grounder will pick the best provider for it on each search."),
    ("Fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder preset Fast: Grounder will pick the best provider for it on each search."),
    ("preset:deep", {"server": None, "server_mode": None, "preset": "deep"}, "Grounder preset Deep: Grounder will pick the best provider for it on each search."),
    ("mode:fast", {"server": None, "server_mode": None, "preset": "fast"}, "Grounder preset Fast: Grounder will pick the best provider for it on each search."),  # what 0.5 called a preset
    ("polymarket", {"server": None, "server_mode": None, "preset": None, "live_data_always": ["polymarket"]},
     "Live-data APIs, in every mode. Every call: Polymarket. All the others: Grounder decides."),
    ("api:polymarket", {"server": None, "server_mode": None, "preset": None, "live_data_always": ["polymarket"]},
     "Live-data APIs, in every mode. Every call: Polymarket. All the others: Grounder decides."),
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


@pytest.mark.parametrize("word", ["auto", "optimized", "OPTIMIZED"])
def test_search_optimized_or_auto_clears_the_pick_and_keeps_the_rest(plugin, signed_in, tmp_path, word):
    _config(tmp_path, search_backend="grounder", grounder={"server": "provider:exa", "server_mode": "deep", "preset": "deep", "live_data_off": ["espn"]})
    host = Host()
    assert plugin.commands.search_command(host, word) == "Grounder optimized: Grounder will pick the provider for each search from your question."
    assert host.asked == []
    assert (_saved(tmp_path, "server"), _saved(tmp_path, "server_mode"), _saved(tmp_path, "preset")) == (None, None, None)
    assert _saved(tmp_path, "live_data_off") == ["espn"]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]["search_backend"] == "grounder"


def test_search_with_an_unknown_argument_or_the_default_search_alias_lists_what_there_is_and_saves_nothing(plugin, signed_in, tmp_path):
    for arg in ("nope", "parallel:fast"):
        reply = plugin.commands.search_command(Host(), arg)
        assert reply.splitlines()[0] == f"Grounder has no preset, provider or mode called {arg}."
        assert "  exa (Exa): instant, deep" in reply and "  polymarket (Polymarket)" in reply and "  fast (Fast): Quick answers" in reply and "`/search optimized`" in reply
        assert _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") is None


def test_an_id_that_is_more_than_one_kind_of_thing_needs_its_prefix(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "x", "display_name": "X search"}], "live_data_apis": [{"id": "x", "display_name": "X posts"}]}
    reply = plugin.commands.search_command(Host(), "x")
    assert reply.splitlines()[0] == "x is more than one thing: use `/search provider:x` or `/search api:x`." and _saved(tmp_path, "server") is None
    assert plugin.commands.search_command(Host(), "api:x") == "Live-data APIs, in every mode. Every call: X posts. All the others: Grounder decides."
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "live_data_always") == ["x"]  # a pick the live-data API setting carries, not a server


def test_when_nothing_can_ask_search_lists_what_there_is(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder optimized (`/search optimized`, the default): Grounder picks the provider for your question.\n"
                            "Grounder preset (`/search <preset>`):\n  fast (Fast): Quick answers\n")
    assert ("Grounder direct (`/search <provider>` or `/search <provider>:<mode>`, the only search provider called), each provider with its modes:\n"
            "  exa (Exa): instant, deep\n  tavily (Tavily)\n  parallel (Parallel): turbo\n") in reply
    assert ("Live-data APIs, each Every call, Never or Grounder decides (`/search apis <id>=every|never|auto`), in every mode:\n"
            "  polymarket (Polymarket): Grounder decides\n  espn (ESPN): Grounder decides\n") in reply and "`/search <id>`" in reply
    assert reply.splitlines()[-1] == ("Use `/search <id>` to choose a Grounder preset or the one search provider to call (Grounder direct; `/search <provider>:<mode>` for one of its modes), "
                                      "`/search optimized` to let Grounder pick the provider, or `/search apis` to set each live-data API to Every call, Never or Grounder decides.")
    assert _saved(tmp_path, "server") is None


# --- a surface that caps a card ----------------------------------------------------------------------------------------

def _many(n, prefix="p"):
    return {"providers": [{"provider": f"{prefix}{i}", "display_name": f"P{i}"} for i in range(n)], "live_data_apis": []}


def test_a_long_list_is_a_page_of_eight_then_more_with_its_range_then_back_and_back_on_a_later_page_goes_to_the_page_before(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    host = Host(["providers", "__more__", "__more__", "back", "back", "provider:p5", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: direct P5"
    cards = [(question, choices) for question, choices in host.asked[1:6]]
    assert [len(choices) for _, choices in cards] == [10, 10, 7, 10, 10]  # eight items, More and Back; the last page has 6 items and Back
    first, second, third = (_labels(choices) for _, choices in cards[:3])
    assert first == [f"P{i}" for i in range(8)] + ["More: 9–16 of 22", "← Back"]
    assert second == [f"P{i}" for i in range(8, 16)] + ["More: 17–22 of 22", "← Back: 1–8 of 22"]
    assert third == [f"P{i}" for i in range(16, 22)] + ["← Back: 9–16 of 22"]  # no More on the last page
    # Back on page 3 went to page 2 and Back on page 2 to page 1; Back on page 1 would have left the list.
    assert _labels(cards[3][1])[0] == "P8" and _labels(cards[4][1])[0] == "P0"
    assert all(question == "Which provider should Grounder direct search with?" for question, _ in cards)
    assert _saved(tmp_path, "server") == "provider:p5"


def test_back_on_the_first_page_leaves_the_list_for_the_hub(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    host = Host(["providers", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder direct search with?", "How should Grounder search?"]


def test_a_list_opens_on_the_page_that_holds_the_current_choice_and_marks_it(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    _config(tmp_path, grounder={"server": "provider:p19"})
    host = Host(["providers", "back", "back", "back", "done"])
    plugin.commands.search_command(host, "")
    labels = _labels(host.asked[1][1])  # the third page, where p19 is
    assert labels[:6] == [f"P{i}" for i in range(16, 19)] + ["P19 (current)", "P20", "P21"] and labels[-1] == "← Back: 9–16 of 22" and "More: 17–22 of 22" not in labels
    assert [_labels(choices)[0] for _, choices in host.asked[2:4]] == ["P8", "P0"]  # Back on a later page is the page before
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa", "offerings": [{"type": "exa", "mode": None}] + [
        {"type": f"exa:m{i}", "mode": f"m{i}", "description": f"Mode {i}"} for i in range(16)]}], "live_data_apis": []}
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "m12"})
    host = Host(["providers", "provider:exa", "back", "back", "back", "done"])
    plugin.commands.search_command(host, "")
    modes = _labels(host.asked[2][1])  # m12 is on the second page of the modes, so that is where the modes open
    assert "m12 (current)" in modes and modes[0] == "Default" and modes[-1] == "← Back: 1–8 of 16" and "More: 17–16 of 16" not in modes


def test_a_providers_modes_are_two_pages_of_eight_with_default_on_each_and_their_own_descriptions(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa", "description": "Exa the engine", "offerings": [
        {"type": "exa", "mode": None, "description": "Exa's standard search"}] + [{"type": f"exa:m{i}", "mode": f"m{i}", "description": f"Mode {i} line"} for i in range(16)]}],
        "live_data_apis": []}
    host = Host(["providers", "provider:exa", "__more__", "mode:exa:m10", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: direct Exa m10"
    first, second = host.asked[2][1], host.asked[3][1]
    assert _labels(first) == ["Default"] + [f"m{i}" for i in range(8)] + ["More: 9–16 of 16", "← Back"]  # Default is on every page, so each is 11 rows
    assert _labels(second) == ["Default"] + [f"m{i}" for i in range(8, 16)] + ["← Back: 1–8 of 16"]
    assert [c["description"] for c in first[:3]] == ["Exa's standard search", "Mode 0 line", "Mode 1 line"]  # the API's own lines, the default's included


def test_a_mode_with_no_description_from_an_older_api_is_made_from_its_note_or_its_features(plugin, signed_in, lists):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa", "description": "Exa the engine.", "offerings": [
        {"type": "exa", "mode": None}, {"type": "exa:a", "mode": "a", "features": ["web-search", "page-text"]},
        {"type": "exa:b", "mode": "b", "limits": "Legacy: use lite.", "features": ["news"]}, {"type": "exa:c", "mode": "c", "limits": "News articles and current reporting. More.", "features": ["news"]}]}],
        "live_data_apis": []}
    catalog, _ = plugin.commands._catalog()
    owner = catalog.provider("exa")
    assert [m["description"] for m in owner["modes"]] == ["Web search, page text", "News", "News articles and current reporting"]
    assert owner["default_description"] == ""
    host = Host(["providers", "provider:exa", "back", "back", "done"])
    plugin.commands.search_command(host, "")
    assert host.asked[2][1][0]["description"] == "Exa the engine"  # no line for the default search: the provider's own description, without its full stop


def test_a_card_a_surface_caps_holds_its_cap_counting_the_other_row_hermes_adds(plugin, signed_in, lists, tmp_path):
    # Discord's buttons stop at 25 and Hermes keeps one for Other; WhatsApp's list takes 10 rows and Hermes appends Other after slicing to 10, so a card holds 9.
    assert plugin.commands.SURFACE_CHOICE_CAPS == {"discord": 24, "whatsapp_cloud": 9}
    lists.providers_body = _many(30)
    host = Host(["providers", "__more__", "__more__", "__more__", "provider:p25", "done"], surface="whatsapp_cloud")
    assert plugin.commands.search_command(host, "") == "Grounder: direct P25"
    cards = [[c["value"] for c in choices] for _, choices in host.asked[1:5]]
    assert all(len(card) <= 9 for card in cards) and [len(card) for card in cards] == [9, 9, 9, 9]  # seven items, More and Back
    assert cards[0][-2:] == ["__more__", "back"] and "provider:p21" in cards[3] and _saved(tmp_path, "server") == "provider:p25"
    _config(tmp_path, grounder={})
    host = Host(["providers", "provider:p3", "done"], surface="discord")
    plugin.commands.search_command(host, "")
    assert len(host.asked[1][1]) == 10  # Discord holds 24: eight items, More and Back


def test_every_page_of_the_live_data_apis_screen_has_set_all_and_back_and_the_list_returns_to_its_page(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa"}], "live_data_apis": [{"id": f"a{n}", "display_name": f"A{n}"} for n in range(20)]}
    host = Host(["apis", "__more__", "__more__", "a17", "always", "back", "back", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Grounder: optimized · Live-data APIs: A17 every call"
    cards = [[c["value"] for c in choices] for _, choices in host.asked[1:4]]
    assert [len(card) for card in cards] == [11, 11, 6]  # Set all, then 8, 8 and 4 APIs, with More and Back (the last page has no More)
    assert all(card[0] == "__all__" and card[-1] == "back" for card in cards)  # Set all first and Back last, on every page
    assert [("__more__" in card) for card in cards] == [True, True, False]
    assert host.asked[5][0] == "Live-data APIs: pick one to change how Grounder uses it." and host.asked[5][1][1]["value"] == "a16"  # asked again on the page it was on
    assert _labels(host.asked[3][1])[-1] == "← Back: 9–16 of 20" and _saved(tmp_path, "live_data_always") == ["a17"] and _saved(tmp_path, "live_data_off") is None
    # WhatsApp: Set all, six APIs, More and Back are nine.
    host = Host(["apis", "back", "done"], surface="whatsapp_cloud")
    plugin.commands.search_command(host, "")
    assert len(host.asked[1][1]) == 9


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
    # The three modes in order, each under its header: Grounder optimized (one row), Grounder preset (a row of presets), Grounder direct (the providers).
    assert pick["choices"][:3] == [{"header": "Grounder optimized"},
                                   {"value": "auto", "label": "Grounder picks the provider for your question",
                                    "description": "The default. The provider's own results, each uncut and in its own order."},
                                   {"header": "Grounder preset", "row": True}]
    headers = [c["header"] for c in pick["choices"] if "header" in c]
    assert headers == ["Grounder optimized", "Grounder preset", "Grounder direct"] and {"header": "Grounder direct"} in pick["choices"]
    assert pick["choices"].index({"header": "Grounder direct"}) == 3 + 6  # after the six presets
    by_value = {c["value"]: c for c in pick["choices"] if "value" in c}
    # A preset's row says what it does and, from /v1/search-profiles, what it includes; a preset that does not say includes only what it does.
    assert by_value["preset:fast"]["description"] == "Quick answers Grounder picks the best provider for this preset. Uses 2 providers and 1 mode."
    assert by_value["preset:cheap"]["description"] == "Lowest cost Grounder picks the best provider for this preset."
    assert by_value["preset:deep"]["description"] == "Thorough research Grounder picks the best provider for this preset. Uses 1 provider and 1 mode."
    assert by_value["preset:fast"]["lights"] == ["mode:exa:instant", "provider:tavily"] and by_value["preset:deep"]["lights"] == ["mode:exa:deep"]
    assert "lights" not in by_value["preset:cheap"]  # an API that does not say what a preset includes lights nothing
    assert by_value["mode:exa:instant"]["parent"] == "provider:exa" and "parent" not in by_value["provider:exa"]
    assert "parallel:fast" not in str(pick) and by_value["provider:exa"]["description"] == "Embeddings search. The only search provider called."
    assert by_value["mode:exa:instant"]["description"] == "Exa's instant mode. The only search provider called." and by_value["provider:tavily"]["description"] == "The only search provider called."
    assert (apis["id"], apis["tab"], apis["multi_select"], apis["one_per_row"], apis["optional"]) == ("apis", "Live-data APIs", True, True, True)
    # Each live-data API is a header over a row of its three settings, one checked: the one it has now (ESPN was set to Never; the other is Grounder decides).
    assert [c if "header" in c else c["value"] for c in apis["choices"]] == [
        {"header": "Polymarket", "row": True}, "polymarket:always", "polymarket:off", "polymarket:auto",
        {"header": "ESPN", "row": True}, "espn:always", "espn:off", "espn:auto"]
    assert apis["checked"] == ["polymarket:auto", "espn:off"] and apis["selected"] == "polymarket:auto"  # the cursor starts on the first API's current setting
    assert [c["label"] for c in apis["choices"] if "value" in c][:3] == ["Every call", "Never", "Grounder decides"]
    assert [c["match"] for c in apis["choices"] if "value" in c][:3] == ["Polymarket Every call", "Polymarket Never", "Polymarket Grounder decides"]  # a filter finds an API by its name
    assert apis["choices"][1]["description"] == "Grounder asks Polymarket on every search, in every mode, beside the search provider."
    assert apis["choices"][3]["description"] == "Grounder asks Polymarket only when your question's features call for it, in every mode. The default."
    assert apis["hint"] == "↑/↓ move · ←/→ + Space sets · type to filter · Tab search · Enter confirms"


def test_what_the_form_answers_is_saved_each_live_api_by_its_one_setting(plugin, signed_in, tmp_path):
    host = Host(form={"pick": "mode:exa:deep", "apis": ["polymarket:auto", "espn:off"]})
    assert plugin.commands.search_command(host, "") == "Grounder direct: Grounder will use only Exa's deep mode as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`). Live-data APIs, in every mode. Never: ESPN. All the others: Grounder decides."
    assert (_saved(tmp_path, "server"), _saved(tmp_path, "server_mode"), _saved(tmp_path, "preset"), _saved(tmp_path, "live_data_off")) == ("provider:exa", "deep", None, ["espn"])
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:always", "espn:auto"]}), "") == (
        "Grounder optimized: Grounder will pick the provider for each search from your question. Live-data APIs, in every mode. Every call: Polymarket. All the others: Grounder decides.")
    assert _saved(tmp_path, "server") is None and _saved(tmp_path, "live_data_off") is None  # nothing set to Never: the key is not kept
    assert _saved(tmp_path, "live_data_always") == ["polymarket"]
    # An API with none of its settings checked, or two of them, is left as it is (a form that did not hold the one-per-row rule).
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:always", "polymarket:off", "espn:never"]}), "") == "Nothing changed."
    assert _saved(tmp_path, "live_data_always") == ["polymarket"] and _saved(tmp_path, "live_data_off") is None


def test_a_form_that_changes_only_the_live_data_page_leaves_the_pick_it_cannot_show_alone(plugin, signed_in, tmp_path):
    for saved in ({"server": "api:espn"}, {"server": "provider:defunct", "server_mode": "x"}, {"preset": "gone"}):
        _config(tmp_path, grounder=saved)
        host = Host(form={"pick": "auto", "apis": ["polymarket:auto", "espn:off"]})  # page one answers as it started: on Grounder optimized, the nearest row
        want = {**saved, "server": None} if saved.get("server") == "api:espn" else saved  # an older version's API server is carried by the settings, so it is cleared
        assert plugin.commands.search_command(host, "") == "Live-data APIs, in every mode. Never: ESPN. All the others: Grounder decides."
        assert {k: _saved(tmp_path, k) for k in saved} == want and _saved(tmp_path, "live_data_off") == ["espn"]
    _config(tmp_path, grounder={"preset": "fast"})
    assert plugin.commands.search_command(Host(form={"pick": "preset:fast", "apis": ["polymarket:auto", "espn:auto"]}), "") == "Nothing changed."
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:auto", "espn:auto"]}), "") == (
        "Grounder optimized: Grounder will pick the provider for each search from your question.")


def test_a_form_of_one_page_has_no_tab_hint(plugin, signed_in, lists, tmp_path):
    only = Host(form={"apis": ["espn:auto", "polymarket:auto"]})
    plugin.commands.search_command(only, "apis")
    assert "Tab" not in only.pages[0]["hint"] and only.pages[0]["hint"] == "↑/↓ move · ←/→ + Space sets · type to filter · Enter confirms"
    lists.providers_body = {**PROVIDERS, "live_data_apis": []}
    host = Host(form={"pick": "auto"})
    plugin.commands.search_command(host, "")
    assert [p["id"] for p in host.pages] == ["pick"] and "Tab" not in host.pages[0]["hint"]  # with no live-data APIs listed there is no second page


def test_an_api_that_is_new_to_grounder_is_grounder_decides_in_the_form_until_the_user_sets_it(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_off": ["espn"]})
    lists.providers_body = {**PROVIDERS, "live_data_apis": [*PROVIDERS["live_data_apis"], {"id": "newcomer", "display_name": "Newcomer"}]}
    host = Host(form=None)
    plugin.commands.search_command(host, "")
    assert host.pages[1]["checked"] == ["polymarket:auto", "espn:off", "newcomer:auto"]


def test_a_cancelled_form_changes_nothing_and_search_apis_is_the_second_page_alone(plugin, signed_in, tmp_path):
    assert plugin.commands.search_command(Host(form=None), "") == "Nothing changed." and not (tmp_path / "config.yaml").exists()
    host = Host(form={"apis": ["espn:always", "polymarket:off"]})
    assert plugin.commands.search_command(host, "apis") == "Live-data APIs, in every mode. Every call: ESPN. Never: Polymarket. All the others: Grounder decides."
    assert [page["id"] for page in host.pages] == ["apis"] and host.pages[0]["optional"] is False
    assert _saved(tmp_path, "live_data_off") == ["polymarket"] and _saved(tmp_path, "live_data_always") == ["espn"]


def test_a_hermes_whose_form_is_not_available_here_asks_in_the_hub(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:fast", "done"], form=None)
    host.supports_form = lambda: False
    assert plugin.commands.search_command(host, "") == "Grounder: preset Fast" and host.pages is None and len(host.asked) == 3


def test_a_saved_api_pin_from_an_earlier_version_starts_the_form_on_grounder_optimized_and_the_next_pick_replaces_it(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"server": "api:polymarket"})
    host = Host(form={"pick": "preset:deep", "apis": ["polymarket", "espn"]})
    assert plugin.commands.search_command(host, "").startswith("Grounder preset Deep: Grounder will pick the best provider for it on each search. It no longer always includes Polymarket.")
    assert host.pages[0]["selected"] == "auto" and _saved(tmp_path, "server") is None and _saved(tmp_path, "preset") == "deep"


# --- the lists come from the API, when asked -------------------------------------------------------------------------------

def test_the_lists_are_read_each_time_so_a_new_provider_or_preset_is_on_offer_at_once(plugin, signed_in, lists):
    plugin.commands.search_command(Host(["auto", "done"]), "")
    lists.providers_body = {"providers": [{"provider": "newcomer", "display_name": "Newcomer"}], "live_data_apis": []}
    lists.profiles_body = {"profiles": [{"id": "later", "name": "Later", "description": "Added since"}]}
    second, third = Host(["providers", "provider:newcomer", "done"]), Host(["presets", "preset:later", "done"])
    plugin.commands.search_command(second, "")
    plugin.commands.search_command(third, "")
    assert [c["value"] for c in second.asked[1][1]] == ["provider:newcomer", "back"]
    assert [c["value"] for c in third.asked[1][1]] == ["preset:later", "back"]
    assert _lists_read(lists) == ["/v1/providers", "/v1/search-profiles"] * 3


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
    lists.list_status = 200
    monkeypatch.setattr(plugin.provider, "PROVIDERS_URL", "http://127.0.0.1:9/v1/providers")
    reply = plugin.commands.search_command(Host(["providers"]), "")
    assert reply.startswith("Could not load Grounder's list of providers: Could not reach Grounder")
    assert _saved(tmp_path, "server") is None


def test_a_sign_in_hermes_refuses_is_said_not_raised(plugin, hermes, lists):
    hermes.raises = plugin.provider.AuthError("grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder", code="oauth_client_id_missing")
    assert plugin.commands.search_command(Host(), "") == "Could not load Grounder's list of providers: Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set."


def test_on_slack_the_hub_runs_with_no_form_and_a_long_list_is_pages_of_eight(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"P{n}"} for n in range(22)], "live_data_apis": []}
    host = Host(["providers", "__more__", "__more__", "provider:p21", "done"], surface="slack")  # `/hermes search`: Slack's own /search is not this command
    assert plugin.commands.search_command(host, "") == "Grounder: direct P21"
    assert [len(choices) for _, choices in host.asked] == [4, 10, 10, 7, 4]
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
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "direct",
                            "routing_settings": {"providers": {"exa": {"use": "always", "modes": {"instant": "always"}}}, "exclusive": True, "fallback": False}}
    assert signed_in.calls[-1]["json"] == _sent(lists)
    plugin.commands.search_command(host, "exa")
    provider.search("q")
    # The pin is exclusive, and `fallback` is sent off with it whatever the account's own setting is: if the provider finds nothing, nothing comes back.
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False} and _sent(lists)["mode"] == "direct"
    plugin.commands.search_command(host, "deep")  # a preset clears the pin
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "preset", "profile": "deep"}
    plugin.commands.search_command(host, "polymarket")
    provider.search("q")
    # A live-data API set to Every call rides beside the preset: it replaces nothing, and nothing exclusive about it.
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "preset", "profile": "deep", "routing_settings": {"specialized": {"polymarket": {"use": "always"}}}}
    plugin.commands.search_command(host, "auto")
    provider.search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "optimized", "routing_settings": {"specialized": {"polymarket": {"use": "always"}}}}  # Grounder optimized keeps it too


def test_each_live_data_api_is_sent_as_it_is_set_and_a_pinned_one_stays_on(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_off": ["espn", "polymarket"], "server": "provider:exa", "server_mode": "deep"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always", "modes": {"deep": "always"}}}, "exclusive": True, "fallback": False,
                                                 "specialized": {"espn": {"use": "off"}, "polymarket": {"use": "off"}}}
    _config(tmp_path, grounder={"live_data_off": ["espn", "polymarket"], "server": "api:espn"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"] == {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "off"}}}
    assert "fallback" not in _sent(lists)["routing_settings"]


def test_a_search_with_a_key_sends_them_too(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "fast", "server": "provider:tavily", "live_data_off": ["espn"]})
    plugin.GrounderWebSearchProvider().search("q")
    # Saved by hand, a preset and a pinned provider are both there: the direct choice wins, so the search is direct and carries no profile.
    assert "profile" not in _sent(lists) and _sent(lists)["mode"] == "direct"
    assert _sent(lists)["routing_settings"] == {"providers": {"tavily": {"use": "always"}}, "exclusive": True, "fallback": False, "specialized": {"espn": {"use": "off"}}}
    _config(tmp_path, grounder={"preset": "fast", "live_data_off": ["espn"]})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["profile"] == "fast" and _sent(lists)["mode"] == "preset"


def test_a_free_search_sends_none_of_what_the_user_chose(plugin, free, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa", "server_mode": "deep", "live_data_off": ["espn"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search_tool({"query": "q"})
    assert len(_searches(lists)) == 2 and all("routing_settings" not in body and "profile" not in body and "exclusive" not in json.dumps(body) for body in _searches(lists))
    assert all("Authorization" not in r["headers"] for r in lists.requests if r["path"] == "/v1/search")


@pytest.mark.parametrize("grounder,routing", [
    ({"preset": "Not An Id!", "server": "exa", "server_mode": "x"}, None),
    ({"preset": 7, "server": ["provider:exa"], "live_data_off": "espn"}, None),
    ({"preset": "", "server": " ", "live_data_off": [3, "Bad Id!", None]}, None),
    ({"server": "provider:exa", "server_mode": 7}, {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False}),  # a mode that is not a string is dropped, the pin stays
    ("deep", None), (None, None)])
def test_a_saved_value_of_another_shape_reads_as_unset(plugin, keyed, lists, tmp_path, grounder, routing):
    _config(tmp_path, grounder=grounder)
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "direct" if routing else "optimized", **({"routing_settings": routing} if routing else {})}


def test_a_config_that_cannot_be_read_changes_nothing_about_a_search(plugin, keyed, lists, monkeypatch):
    import hermes_cli.config as config

    def broken():
        raise OSError("unreadable")

    monkeypatch.setattr(config, "load_config", broken)
    assert plugin.GrounderWebSearchProvider().search("q")["success"]
    assert _sent(lists) == {"query": "q", "top_k": 5, "mode": "optimized"}


def test_the_user_chose_wins_over_what_the_model_passes_for_the_same_provider_or_api(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"preset": "deep", "server": "provider:exa", "live_data_off": ["espn"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q"})
    assert "profile" not in _sent(lists) and _sent(lists)["mode"] == "direct"  # both saved by hand: the pin wins, and a direct search has no preset
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False, "specialized": {"espn": {"use": "off"}}}
    # What the model says about other settings is added, and the user's stand where both speak: a model cannot make a pin inclusive or switch its fallback on,
    # nor put a preset of its own over a provider the user chose.
    provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"caps": {"search": {"sources": 5}}}})
    assert "profile" not in _sent(lists) and _sent(lists)["mode"] == "direct"
    assert _sent(lists)["routing_settings"] == {"caps": {"search": {"sources": 5}}, "providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False, "specialized": {"espn": {"use": "off"}}}
    provider.search_tool({"query": "q", "routing_settings": {"fallback": True, "exclusive": False}})
    assert _sent(lists)["routing_settings"] == {"providers": {"exa": {"use": "always"}}, "exclusive": True, "fallback": False, "specialized": {"espn": {"use": "off"}}}
    provider.search_tool({"query": "q", "routing_settings": {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "always"}},
                                                                   "providers": {"exa": {"use": "off"}, "brave": {"use": "always"}}}})
    assert _sent(lists)["routing_settings"] == {"specialized": {"espn": {"use": "off"}, "polymarket": {"use": "always"}}, "exclusive": True, "fallback": False,
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
     {"server": "provider:exa", "server_mode": None}, "the `bogusmode` mode of exa you chose to search with"),
    ({"server": "provider:defunct", "server_mode": "x"}, "No such provider: defunct", {"server": None, "server_mode": None}, "the provider `defunct` you chose to search with"),
    ({"server": "api:gone-api"}, "No such specialized API: gone-api", {"server": None}, "the live-data API `gone-api` you always included"),
    ({"live_data_off": ["gone-api", "espn"]}, "No such specialized API: gone-api", {"live_data_off": ["espn"]}, "the live-data API `gone-api` you set to Never"),
    ({"live_data_always": ["gone-api", "espn"]}, "No such specialized API: gone-api", {"live_data_always": ["espn"]}, "the live-data API `gone-api` you set to Every call"),
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
    assert "the preset `turbo` or the provider `defunct` you chose to search with" in data["notice"] and len(_searches(lists)) == 3
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


# --- an exclusive pin (Grounder 0.8.16) -------------------------------------------------------------------------------------

@pytest.mark.parametrize("saved,exclusive", [
    ({"server": "provider:exa"}, True),
    ({"server": "provider:exa", "server_mode": "deep"}, True),
    ({"server": "provider:exa", "preset": "fast"}, True),  # both saved by hand: the pin goes, as it always did, and is exclusive
    ({"preset": "fast"}, False),
    ({}, False),
    ({"server": "api:espn"}, False),  # a pinned live-data API stays as it was
    ({"live_data_off": ["espn"]}, False),
])
def test_only_a_pinned_provider_makes_a_search_exclusive_and_it_always_sends_fallback_off_with_it(plugin, keyed, lists, tmp_path, saved, exclusive):
    _config(tmp_path, grounder=saved)
    plugin.GrounderWebSearchProvider().search("q")
    routing = _sent(lists).get("routing_settings", {})
    assert (routing.get("exclusive") is True and routing.get("fallback") is False) is exclusive
    if not exclusive:
        assert "exclusive" not in json.dumps(_sent(lists)) and "fallback" not in routing


def test_a_search_the_user_chose_nothing_for_sends_no_exclusive_and_neither_does_a_preset_or_auto(plugin, signed_in, lists, tmp_path):
    host = Host()
    provider = plugin.GrounderWebSearchProvider()
    for choice in ("fast", "auto"):
        plugin.commands.search_command(host, choice)
        provider.search("q")
        assert "exclusive" not in json.dumps(_sent(lists))
    plugin.commands.search_command(host, "exa")
    provider.search("q")
    assert _sent(lists)["routing_settings"]["exclusive"] is True
    plugin.commands.search_command(host, "auto")  # "Let Grounder pick" clears it
    provider.search("q")
    assert "exclusive" not in json.dumps(_sent(lists)) and "routing_settings" not in _sent(lists)


@pytest.mark.parametrize("fixture_name", ["keyed", "signed_in"])
def test_against_a_grounder_from_before_exclusive_the_pin_is_sent_once_as_always_included_and_the_result_says_so(request, plugin, lists, tmp_path, fixture_name):
    request.getfixturevalue(fixture_name)
    _config(tmp_path, grounder={"server": "provider:exa", "server_mode": "instant", "live_data_off": ["espn"]})
    lists.search_script = [_refused("Unknown setting: exclusive"), _OK]
    data = json.loads(plugin.GrounderWebSearchProvider().search_tool({"query": "q"}))["data"]
    assert len(data["web"]) == 1
    first, second = _searches(lists)
    assert len(_searches(lists)) == 2 and first["query"] == second["query"] == "q"
    pinned = {"providers": {"exa": {"use": "always", "modes": {"instant": "always"}}}, "specialized": {"espn": {"use": "off"}}}
    assert first["routing_settings"] == {**pinned, "exclusive": True, "fallback": False}
    assert second["routing_settings"] == pinned  # the older form: always included, nothing else changed
    assert "cannot yet search with only the provider the user chose" in data["notice"] and "Tell the user this" in data["notice"]
    assert "no longer offers" not in data["notice"]
    # The choice is the user's and stays: the next search tries exclusive again.
    assert _saved(tmp_path, "server") == "provider:exa" and _saved(tmp_path, "server_mode") == "instant"
    lists.search_script = [_OK]
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"]["exclusive"] is True


def test_the_older_form_is_tried_once_and_only_for_a_400_that_does_not_know_exclusive(plugin, keyed, lists, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa"})
    provider = plugin.GrounderWebSearchProvider()
    lists.search_script = [_refused("Unknown setting: exclusive"), _refused("Unknown setting: exclusive")]
    assert provider.search("q") == {"success": False, "error": "Grounder returned HTTP 400: Unknown setting: exclusive"}
    assert len(_searches(lists)) == 2  # one more, never a loop
    lists.requests.clear()
    # A newer Grounder's own 400 about exclusive is not that: nothing is sent again.
    lists.search_script = [_refused("exclusive needs a provider (or one of its modes) set to always: the search then calls only those.")]
    assert provider.search("q")["success"] is False
    assert len(_searches(lists)) == 1
    lists.requests.clear()
    # And a pin that is refused for what it names is dropped as before, with the exclusive form still the one sent.
    lists.search_script = [_refused("No such provider: exa"), _OK]
    data = provider.search("q")["data"]
    assert "the provider `exa` you chose to search with" in data["notice"] and "cannot yet" not in data["notice"]


def test_a_free_search_never_retries_for_exclusive(plugin, free, lists, tmp_path):
    _config(tmp_path, grounder={"server": "provider:exa"})
    lists.search_script = [_refused("Unknown setting: exclusive")]
    assert plugin.GrounderWebSearchProvider().search("q")["success"] is False
    assert len(_searches(lists)) == 1 and "exclusive" not in json.dumps(_searches(lists)[0])


def test_the_direct_words_follow_the_fallback_switch(plugin, signed_in, lists, monkeypatch):
    assert plugin.commands.search_command(Host(), "exa") == "Grounder direct: Grounder will use only Exa as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."
    monkeypatch.setattr(plugin.provider, "DIRECT_FALLBACK", True)
    assert plugin.commands.search_command(Host(), "exa") == "Grounder direct: Grounder will use only Exa as the search provider; if it finds nothing, Grounder picks for you. Live-data APIs follow your live-data choices (`/search apis`)."


def test_the_words_everywhere_say_only_that_provider_and_that_what_it_finds_is_what_you_get_even_when_that_is_nothing(plugin, signed_in, lists):
    assert plugin.commands.PIN_NOTE == "The only search provider called."
    host = Host()
    plugin.commands.search_command(host, "exa:instant")
    assert "only Exa's instant mode as the search provider and return what it finds, even when that is nothing." in plugin.commands.search_command(Host(), "exa:instant")
    docs = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md")}
    docs["plugin.yaml"] = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name, text in docs.items():
        assert "to always include" not in text and "always included in a search" not in text, name
        assert "even when that is nothing" in text and "Grounder picks for you" not in text, name
    source = "\n".join((PLUGIN_DIR / name).read_text() for name in ("commands.py", "provider.py"))
    assert "Grounder will always include" not in source and "will search with only" not in source


# --- Grounder's three modes: optimized, preset, direct --------------------------------------------------------------------

def test_the_three_modes_are_named_by_the_constants_the_hermes_copy_uses(plugin):
    commands = plugin.commands
    assert (commands.OPTIMIZED_HEADER, commands.PRESETS_HEADER, commands.PROVIDERS_HEADER) == ("Grounder optimized", "Grounder preset", "Grounder direct")
    assert commands.AUTO_LABEL == "Grounder picks the provider for your question"
    assert commands.AUTO_NOTE == "The default. The provider's own results, each uncut and in its own order."
    assert commands.PRESET_NOTE == "Grounder picks the best provider for this preset."
    assert commands.PIN_NOTE == "The only search provider called."


def test_every_document_and_docstring_names_the_three_modes_and_none_says_the_old_words(plugin):
    texts = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md", "commands.py", "provider.py", "__init__.py")}
    texts["plugin.yaml"] = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name, text in texts.items():
        for mode in ("Grounder optimized", "Grounder preset", "Grounder direct"):
            assert mode in text, (name, mode)
        assert "based on your prompt" not in text and "Let Grounder pick for you" not in text and "let Grounder pick for you" not in text, name
    for name in ("README.md", "after-install.md", "plugin.yaml"):
        assert "uncut" in texts[name] and "reranked" in texts[name], name  # every mode returns what the provider returns
    readme = texts["README.md"]
    assert all(f"| **{mode}** |" in readme for mode in ("Grounder optimized", "Grounder preset", "Grounder direct"))  # the table of the three modes
    for stale in ("800 characters", "6,000", "30,000", "20 rows", "two notes", "saves no page to a file"):
        assert not any(stale in text for name, text in texts.items() if name in ("README.md", "after-install.md", "plugin.yaml")), stale


def test_the_screens_that_ask_for_a_provider_and_a_mode_say_which_mode_they_are_choosing(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "mode:exa:deep", "done"])
    plugin.commands.search_command(host, "")
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder direct search with?", "Which Exa mode?",
                                                        "How should Grounder search?"]
    assert host.asked[0][1][2]["label"] == "A provider" and host.asked[3][1][2]["label"] == "A provider: Exa deep (current)"


def test_a_pick_words_still_say_grounder_direct_for_a_mode_or_provider_grounder_no_longer_lists(plugin, signed_in):
    catalog, _ = plugin.commands._catalog()
    saved = plugin.provider.settings()
    assert plugin.commands._pick_words("mode:exa:gone", catalog, saved) == "Grounder direct: Grounder will use only exa:gone as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."
    assert plugin.commands._pick_words("provider:gone", catalog, saved) == "Grounder direct: Grounder will use only gone as the search provider and return what it finds, even when that is nothing. Live-data APIs follow your live-data choices (`/search apis`)."
    assert plugin.commands._pick_words("preset:gone", catalog, saved) == "Grounder preset gone: Grounder will pick the best provider for it on each search."


# --- a live-data API: Every call, Never or Grounder decides, in every mode -------------------------------------------------------

_MODES = {"optimized": {}, "preset": {"preset": "fast"}, "direct": {"server": "provider:exa", "server_mode": "deep"}}
_SETTINGS = {"always": {"use": "always"}, "off": {"use": "off"}, "auto": None}


@pytest.mark.parametrize("mode", list(_MODES))
@pytest.mark.parametrize("setting", list(_SETTINGS))
def test_a_live_data_api_setting_is_sent_in_every_mode_and_grounder_decides_sends_nothing(plugin, signed_in, lists, tmp_path, mode, setting):
    saved = dict(_MODES[mode])
    if setting != "auto":
        saved["live_data_always" if setting == "always" else "live_data_off"] = ["espn"]
    _config(tmp_path, grounder=saved)
    plugin.GrounderWebSearchProvider().search("q")
    body = _sent(lists)
    assert body["mode"] == mode
    sent = (body.get("routing_settings") or {}).get("specialized", {}).get("espn")
    assert sent == _SETTINGS[setting] and "polymarket" not in (body.get("routing_settings") or {}).get("specialized", {})
    if mode == "direct":  # the search provider is the one picked, exclusive and with no fallback, whatever the live-data APIs say
        assert body["routing_settings"]["providers"] == {"exa": {"use": "always", "modes": {"deep": "always"}}} and body["routing_settings"]["exclusive"] is True
        assert body["routing_settings"]["fallback"] is False
    else:
        assert "exclusive" not in (body.get("routing_settings") or {})
    assert ("profile" in body) == (mode == "preset")


def test_every_call_and_never_together_send_each_api_its_own_setting_in_one_search(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_always": ["espn"], "live_data_off": ["polymarket"], "server": "provider:tavily"})
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"]["specialized"] == {"espn": {"use": "always"}, "polymarket": {"use": "off"}}


def test_an_api_in_both_lists_is_never_and_a_list_of_another_shape_reads_as_none(plugin, signed_in, tmp_path):
    _config(tmp_path, grounder={"live_data_always": ["espn", "polymarket", 3, "Bad Id!"], "live_data_off": ["espn"]})
    saved = plugin.provider.settings()
    assert saved["off"] == ["espn"] and saved["always"] == ["polymarket"]
    _config(tmp_path, grounder={"live_data_always": "espn", "live_data_off": {"espn": True}})
    assert plugin.provider.settings()["always"] == [] and plugin.provider.settings()["off"] == []


def test_what_the_user_set_wins_over_what_the_model_passes_but_grounder_decides_leaves_it_to_the_model(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_always": ["espn"], "live_data_off": ["polymarket"]})
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", "routing_settings": {"specialized": {"espn": {"use": "off"}, "polymarket": {"use": "always"}, "kalshi": {"use": "always"}}}})
    assert _sent(lists)["routing_settings"]["specialized"] == {"espn": {"use": "always"}, "polymarket": {"use": "off"}, "kalshi": {"use": "always"}}
    _config(tmp_path, grounder={})
    provider.search_tool({"query": "q", "routing_settings": {"specialized": {"espn": {"use": "always"}}}})
    assert _sent(lists)["routing_settings"]["specialized"] == {"espn": {"use": "always"}}  # nothing set here: Grounder decides, so the model's own choice stands


def test_a_free_search_sends_no_live_data_setting(plugin, free, lists, tmp_path):
    _config(tmp_path, grounder={"live_data_always": ["espn"], "live_data_off": ["polymarket"]})
    plugin.GrounderWebSearchProvider().search("q")
    assert "routing_settings" not in _sent(lists) and "mode" not in _sent(lists)


def test_search_apis_with_arguments_sets_apis_without_the_page_and_saves_every_pair_or_none(plugin, signed_in, tmp_path):
    host = Host()
    assert plugin.commands.search_command(host, "apis espn=every polymarket=never") == (
        "Live-data APIs, in every mode. Every call: ESPN. Never: Polymarket. All the others: Grounder decides.")
    assert _saved(tmp_path, "live_data_always") == ["espn"] and _saved(tmp_path, "live_data_off") == ["polymarket"] and host.asked == []
    assert plugin.commands.search_command(host, "apis ESPN:auto") == "Live-data APIs, in every mode. Never: Polymarket. All the others: Grounder decides."  # a name, a colon, any case
    assert _saved(tmp_path, "live_data_always") is None
    before = (_saved(tmp_path, "live_data_always"), _saved(tmp_path, "live_data_off"))
    reply = plugin.commands.search_command(host, "apis espn=every nonesuch=never")
    assert reply.startswith("Grounder has no live-data API called nonesuch.\n") and "  espn (ESPN): Grounder decides" in reply
    reply = plugin.commands.search_command(host, "apis espn=sometimes")
    assert reply == "`espn=sometimes` is not a setting: use `espn=every`, `espn=never` or `espn=auto` (Grounder decides)."
    assert (_saved(tmp_path, "live_data_always"), _saved(tmp_path, "live_data_off")) == before  # a pair that is wrong saves none of them
    for word, state in (("every", "always"), ("always", "always"), ("never", "off"), ("off", "off"), ("decides", "auto"), ("auto", "auto")):
        plugin.commands.search_command(host, f"apis espn={word}")
        assert plugin.commands._live_state(plugin.provider.settings(), "espn") == state, word


def test_an_older_api_server_counts_as_every_call_and_the_first_live_data_change_carries_it(plugin, signed_in, lists, tmp_path):
    _config(tmp_path, grounder={"server": "api:espn", "live_data_off": ["polymarket"]})
    assert plugin.commands._live_state(plugin.provider.settings(), "espn") == "always"
    plugin.GrounderWebSearchProvider().search("q")
    assert _sent(lists)["routing_settings"] == {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "off"}}} and _sent(lists)["mode"] == "optimized"
    plugin.commands.search_command(Host(), "apis espn=never")
    assert _saved(tmp_path, "server") is None and sorted(_saved(tmp_path, "live_data_off")) == ["espn", "polymarket"] and _saved(tmp_path, "live_data_always") is None


def test_the_second_page_hint_names_the_keys_that_set_a_choice_and_every_word_says_the_three_settings(plugin):
    commands = plugin.commands
    assert "←/→ + Space sets" in commands.APIS_HINT and "Tab search" in commands.APIS_HINT and "Enter confirms" in commands.APIS_HINT
    assert [label for _, label in commands.LIVE_STATES] == ["Every call", "Never", "Grounder decides"]
    texts = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md", "commands.py", "provider.py")}
    texts["plugin.yaml"] = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name, text in texts.items():
        assert "Every call" in text and "Never" in text and "Grounder decides" in text, name
        assert "which live-data APIs may" not in text and "may use" not in text and "turn one off" not in text and "turned off" not in text, name
    # Grounder direct changes only the search provider; the words never say how Grounder optimized picks.
    for name in ("README.md", "after-install.md", "plugin.yaml"):
        assert "only that provider" not in texts[name] and "only this provider" not in texts[name].lower(), name
        assert "the only search provider called" in texts[name], name
    assert "when Grounder decides" in plugin.provider.GrounderWebSearchProvider().get_setup_schema()["tag"]
