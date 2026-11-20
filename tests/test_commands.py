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


def _saved(stub, key):
    """What the account has saved, in the words /search keeps it in: ``preset``, ``server`` (``provider:<id>``), ``server_mode``, and the live-data APIs saved as Never and as Every call."""
    search = stub.saved_search
    live = lambda use: sorted(ident for ident, saved in stub.saved_live.items() if saved == use) or None
    return {"preset": search["profile"] if search["mode"] == "preset" else None,
            "server": f"provider:{search['requested_provider']}" if search["mode"] == "direct" else None,
            "server_mode": search["provider_mode"] if search["mode"] == "direct" else None,
            "fallback": search["fallback"], "live_data_off": live("off"), "live_data_always": live("always")}[key]


def _account(stub, preset=None, server=None, server_mode=None, live_data_off=(), live_data_always=(), fallback=False):
    """Set the account's saved setting as the site's Routing page would have left it (no validation: a provider or preset Grounder no longer lists can be saved)."""
    if preset:
        stub.saved_search = {"mode": "preset", "profile": preset, "requested_provider": None, "provider_mode": None, "fallback": False}
    elif server:
        stub.saved_search = {"mode": "direct", "profile": None, "requested_provider": server.partition(":")[2], "provider_mode": server_mode, "fallback": fallback}
    else:
        stub.saved_search = {"mode": "optimized", "profile": None, "requested_provider": None, "provider_mode": None, "fallback": False}
    stub.saved_live = {**{ident: "off" for ident in live_data_off}, **{ident: "always" for ident in live_data_always}}


def _puts(stub):
    """The bodies the plugin wrote to the account's setting, in order."""
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search/settings" and r["method"] == "PUT"]


def _reads(stub):
    return [r for r in stub.requests if r["path"] == "/v1/search/settings" and r["method"] == "GET"]


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
    assert set(host.commands) == {"search", "search-direct-format", "search-login", "search-logout"}  # the presets are a choice in /search's first picker, not a command
    assert host.commands["search"].args_hint.endswith("optimized|preset|provider|provider:mode|apis]")
    assert host.commands["search"].description == ("Choose how Grounder searches: Grounder optimized, a Grounder preset, Grounder pinned (one provider and its mode) or Grounder direct (by itself, or as an add-on), "
                                                   "and set each live-data API to Every call, Never or Grounder decides (on Slack: /hermes search)")
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


def test_the_registered_handlers_run_the_commands(plugin, lists, signed_in, tmp_path):
    host = Host()
    plugin.register(host)
    assert host.commands["search"].handler("exa") == _direct_reply("Exa", *EXA_DEFAULT)
    assert host.commands["search"].handler("deep") == _preset_reply("Deep", "Thorough research", "1 provider and 1 mode")
    assert _saved(lists, "server") is None and _saved(lists, "preset") == "deep"  # a preset clears a pinned provider


# --- /search is registered only while Grounder is the web search backend -------------------------------------------------

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


def _row_text(n, choice):
    """A card's row as Hermes draws it: its number, the label and, after " — ", the description."""
    return f"{n}. {choice['label']}" + (f" — {choice['description']}" if choice.get("description") else "")


SAVED = "(saved to your Grounder account; applies wherever it searches)"
TWO_APIS = "2 on Grounder decides"


def _block(headline, *lines, apis=TWO_APIS):
    """A reply in the shape of Hermes's own "Model switched": a headline, a line for each thing set, the live-data APIs where Grounder lists any, and where it is saved."""
    live = [f"Live-data APIs: {apis}" + (" (`/search apis`)" if len(apis) + 35 <= 64 else "")] if apis else []  # the command that sets them, where the line has room
    return "\n".join([f"✓ {headline}", *(f"  {line}" for line in [*lines, *live, SAVED])])


def _search(mode, changed=True):
    return f"Search {'switched' if changed else 'unchanged'}: {mode}"


def _optimized_reply(apis=TWO_APIS, changed=True):
    return _block(_search("Grounder optimized", changed), "Provider: Grounder picks the provider for your question", "Results: the provider's own, each uncut and in its own order", apis=apis)


def _preset_reply(name, purpose="", uses="", apis=TWO_APIS, changed=True):
    return _block(_search("Grounder preset", changed), f"Preset: {name}", *([f"For: {purpose}"] if purpose else []),
                  "Provider: Grounder picks the best provider for this preset", *([f"Draws from: {uses}"] if uses else []), apis=apis)


def _direct_reply(name, *lines, fallback=False, apis=TWO_APIS, changed=True):
    return _block(_search("Grounder pinned", changed), f"Provider: {name}, the only search provider called", *lines,
                  "Results: what it finds; if nothing, Grounder picks one other" if fallback else "Results: only what it finds, even when that is nothing", apis=apis)


def _apis_reply(every=(), never=()):
    sets = [f"{label}: {', '.join(names)}" for label, names in (("Every call", every), ("Never", never)) if names]
    return "\n".join(["✓ Live-data APIs set, in every mode", *(f"  {line}" for line in [*sets, f"Grounder decides: {'all the others' if sets else 'each one'}", SAVED])])


EXA_DEFAULT, EXA_INSTANT, EXA_DEEP = ("Mode: Default · Embeddings search",), ("Mode: instant · Web search, page text", "Capabilities: web search, page text"), (
    "Mode: deep · A research pass over many pages", "Capabilities: deep research")
FAST = ("Fast", "Quick answers", "2 providers and 1 mode")


def test_search_opens_the_hub_with_the_current_choice_marked_and_says_nothing_changed_when_nothing_did(plugin, lists, signed_in, tmp_path):
    host = Host(["done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."  # no summary line, for no change
    (question, choices), = host.asked
    assert question == "How should Grounder search?"
    assert _values(choices) == ["auto", "presets", "providers", "dev", "apis", "settings", "done"]
    assert _labels(choices) == ["Grounder optimized (current)", "Grounder presets", "Grounder pinned", "Grounder direct", "Live-data APIs", "Settings", "Done"]
    assert [c.get("description") for c in choices] == ["picks per question", "Fast, Cheap, Live and 3 more", "Exa, Tavily and Parallel", "only the address you give", "2 on Grounder decides", "resolver: your own", None]
    # Signed in, the lists are read through Hermes, which attaches the token: the plugin adds no credential, and spends no search.
    # The account's setting is read last, with the same sign-in.
    assert [(c["method"], c["url"]) for c in signed_in.calls] == [("GET", "https://api.grounder.net/v1/providers"), ("GET", "https://api.grounder.net/v1/search-profiles"),
                                                                 ("GET", "https://api.grounder.net/v1/search/settings")]
    assert not {name.lower() for call in signed_in.calls for name in call["headers"]} & {"authorization", "cookie"}
    assert {r["path"] for r in signed_in.stub.requests} == {"/v1/providers", "/v1/search-profiles", "/v1/search/settings"}
    assert [r["method"] for r in signed_in.stub.requests] == ["GET"] * 3  # opening and leaving the hub writes nothing
    assert not (tmp_path / "config.yaml").exists()


def test_a_hub_with_no_live_data_apis_has_no_row_for_them(plugin, signed_in, lists):
    lists.providers_body = {"providers": PROVIDERS["providers"], "live_data_apis": []}
    host = Host(["done"])
    plugin.commands.search_command(host, "")
    assert _values(host.asked[0][1]) == ["auto", "presets", "providers", "dev", "settings", "done"]


def test_a_preset_is_picked_on_its_own_screen_and_the_menu_closes_with_what_is_set_now(plugin, lists, signed_in, tmp_path):
    host = Host(["presets", "preset:fast"])
    assert plugin.commands.search_command(host, "") == _preset_reply(*FAST)
    (_, hub), (question, presets), (direct, _) = host.asked  # the pick, then whether Grounder direct goes with it, and no card after
    assert direct == "Also read sites you name directly, as you?"
    assert question == "Which Grounder preset?"
    assert _values(presets) == ["preset:fast", "preset:cheap", "preset:live", "preset:deep", "preset:code", "preset:balanced", "back"]
    assert _labels(presets)[-1] == "← Back" and _labels(presets)[:2] == ["Fast", "Cheap"]
    assert [c["description"] for c in presets[:4]] == ["Quick answers · 2 providers", "Lowest cost", "Fresh data", "Thorough research · 1 provider"]  # no includes: the purpose stands alone
    assert _labels(hub) == ["Grounder optimized (current)", "Grounder presets", "Grounder pinned", "Grounder direct", "Live-data APIs", "Settings", "Done"]
    assert _saved(lists, "preset") == "fast" and _saved(lists, "server") is None
    again = Host(["done"])  # the next time it opens, the hub marks it and counts the presets where it named them
    plugin.commands.search_command(again, "")
    assert _labels(again.asked[0][1]) == ["Grounder optimized", "Grounder presets: Fast (current)", "Grounder pinned", "Grounder direct", "Live-data APIs", "Settings", "Done"] and again.asked[0][1][1]["description"] == "6 presets"


def test_the_preset_screen_marks_the_current_preset_and_back_returns_to_the_hub_with_nothing_saved(plugin, lists, signed_in, tmp_path):
    _account(lists, **{"preset": "deep"})
    host = Host(["presets", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert _labels(host.asked[1][1])[3] == "Deep (current)" and _labels(host.asked[0][1])[:2] == ["Grounder optimized", "Grounder presets: Deep (current)"]
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which Grounder preset?", "How should Grounder search?"]
    assert _saved(lists, "preset") == "deep"


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


def test_a_provider_is_picked_then_its_mode_and_the_menu_closes_naming_both(plugin, lists, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "mode:exa:instant"])
    reply = plugin.commands.search_command(host, "")
    (_, _), (question, providers), (mode_question, modes), (_, _) = host.asked  # after the mode, only whether Grounder direct goes with it
    assert question == "Which provider should Grounder pin?"
    assert _values(providers) == ["provider:exa", "provider:tavily", "provider:parallel", "back"]
    assert _labels(providers) == ["Exa", "Tavily", "Parallel", "← Back"]  # a provider's name typed picks it
    assert [c.get("description") for c in providers[:3]] == ["2 modes", None, "1 mode"]
    assert mode_question == "Which Exa mode?"
    assert _values(modes) == ["provider:exa", "mode:exa:instant", "mode:exa:deep", "back"]
    assert _labels(modes) == ["Default", "instant", "deep", "← Back"]
    assert [c["description"] for c in modes[:3]] == ["Embeddings search", "Web search, page text", "A research pass over many pages"]
    assert reply == _direct_reply("Exa", *EXA_INSTANT)
    assert _saved(lists, "server") == "provider:exa" and _saved(lists, "server_mode") == "instant" and _saved(lists, "preset") is None
    again = Host(["done"])
    plugin.commands.search_command(again, "")
    hub = again.asked[0][1]
    assert _labels(hub)[:3] == ["Grounder optimized", "Grounder presets", "Grounder pinned: Exa instant (current)"] and hub[2]["description"] == "3 providers"


def test_a_mode_screens_back_returns_to_the_providers_and_that_screens_back_to_the_hub(plugin, lists, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "back", "back", "providers", "provider:tavily"])
    plugin.commands.search_command(host, "")
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder pin?", "Which Exa mode?",
                                                        "Which provider should Grounder pin?", "How should Grounder search?",
                                                        "Which provider should Grounder pin?", "Also read sites you name directly, as you?"]
    assert _saved(lists, "server") == "provider:tavily" and _saved(lists, "server_mode") is None  # a provider with no modes is picked in one step


def test_the_mode_screen_marks_default_or_the_mode_that_is_current_and_a_default_search_named_as_a_mode_is_not_offered(plugin, lists, signed_in, tmp_path):
    _account(lists, **{"server": "provider:exa", "server_mode": "deep"})
    host = Host(["providers", "provider:exa", "provider:exa"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[2][1]) == ["Default", "instant", "deep (current)", "← Back"]
    assert _saved(lists, "server") == "provider:exa" and _saved(lists, "server_mode") is None  # Default keeps the provider and drops a mode pinned before
    _account(lists, **{"server": "provider:exa"})
    host = Host(["providers", "provider:exa", "back", "back", "done"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[2][1])[0] == "Default (current)"
    host = Host(["providers", "provider:parallel", "provider:parallel"])
    plugin.commands.search_command(host, "")
    assert _values(host.asked[2][1]) == ["provider:parallel", "mode:parallel:turbo", "back"]  # parallel:fast is Parallel itself
    assert _saved(lists, "server") == "provider:parallel" and _saved(lists, "server_mode") is None


def test_the_live_apis_screen_sets_all_or_one_api_and_each_change_is_saved_at_once(plugin, lists, signed_in, tmp_path):
    host = Host(["apis", "polymarket", "off", "espn", "always", "back"])
    reply = plugin.commands.search_command(host, "")
    questions = [question for question, _ in host.asked]
    assert questions == ["How should Grounder search?", "Live-data APIs: pick one to change how Grounder uses it.", "How should Grounder use Polymarket?",
                         "Live-data APIs: pick one to change how Grounder uses it.", "How should Grounder use ESPN?", "Live-data APIs: pick one to change how Grounder uses it."]
    first = host.asked[1][1]
    assert _values(first) == ["__all__", "polymarket", "espn", "back"] and _labels(first) == ["Set all to Grounder decides", "Polymarket", "ESPN", "← Back"]
    assert [c.get("description") for c in first[1:3]] == ["Grounder decides", "Grounder decides"]
    one = host.asked[2][1]
    assert _values(one) == ["always", "auto", "off", "back"] and _labels(one) == ["Every call", "Grounder decides (current)", "Never", "← Back"]
    assert [c["description"] for c in one[:3]] == ["on every search, in every mode", "when a question needs it", "never asked, in any mode"]
    assert [c.get("description") for c in host.asked[3][1][1:3]] == ["Never", "Grounder decides"]  # the list comes back with the new setting on its row
    assert _labels(host.asked[3][1])[-1] == "Done"  # a setting differs from when the screen opened, so the way out now closes the menu
    assert _labels(host.asked[4][1])[0] == "Every call" and _saved(lists, "live_data_off") == ["polymarket"] and _saved(lists, "live_data_always") == ["espn"]
    assert reply == _apis_reply(every=["ESPN"], never=["Polymarket"])
    again = Host(["done"])
    plugin.commands.search_command(again, "")
    assert again.asked[0][1][4] == {"value": "apis", "label": "Live-data APIs", "description": "1 on Every call, 1 on Never"}  # a setting nothing is on is left out


def test_set_all_returns_every_api_to_grounder_decides_and_the_hub_counts_them(plugin, lists, signed_in, tmp_path):
    _account(lists, **{"live_data_off": ["polymarket"], "live_data_always": ["espn"]})
    host = Host(["apis", "__all__", "back"])
    reply = plugin.commands.search_command(host, "")
    assert [c.get("description") for c in host.asked[1][1][1:3]] == ["Never", "Every call"] and [c.get("description") for c in host.asked[2][1][1:3]] == ["Grounder decides"] * 2
    assert host.asked[0][1][4]["description"] == "1 on Every call, 1 on Never"
    assert _saved(lists, "live_data_off") is None and _saved(lists, "live_data_always") is None
    assert reply == _apis_reply()  # each one is back on Grounder decides
    again = Host(["done"])
    plugin.commands.search_command(again, "")
    assert again.asked[0][1][4]["description"] == "2 on Grounder decides"


def test_done_or_a_screen_left_as_it_was_says_nothing_changed_and_a_pick_already_set_says_unchanged(plugin, lists, signed_in, tmp_path):
    host = Host(["apis", "espn", "always", "espn", "auto", "back", "done"])  # changed, and changed back: nothing differs from when the screen opened
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert _labels(host.asked[3][1])[-1] == "Done" and _labels(host.asked[5][1])[-1] == "← Back"  # so its way out is Back again, to the hub, whose Done ends it
    assert host.asked[6][0] == "How should Grounder search?"
    _account(lists, **{})
    host = Host(["auto"])  # the pick is already what is set: it is still written, and the reply says it was
    assert plugin.commands.search_command(host, "") == _optimized_reply(changed=False)
    assert _puts(lists)[-1] == {"search": {"mode": "optimized"}}


def test_what_is_set_now_is_the_commands_reply_and_never_posted_through_post_message(plugin, signed_in, tmp_path):
    # Released Hermes's classic CLI runs the command in its gateway, where ctx.post_message answers ok and shows nothing: only a reply is seen on every surface.
    host = Host(["presets", "preset:code"])
    host.post_message = lambda text: (_ for _ in ()).throw(AssertionError("what is set now is a reply, not a post"))
    assert plugin.commands.search_command(host, "") == _preset_reply("Code", "Software questions")


def test_leaving_the_hub_says_nothing_changed_and_a_screen_the_user_leaves_closes_the_menu(plugin, signed_in, tmp_path):
    host = Host(["presets", "back", None])  # back at the hub, which is left, not answered
    assert plugin.commands.search_command(host, "") == "Nothing changed." and len(host.asked) == 3
    for screen in ("providers", "apis", "presets"):
        host = Host([screen, None])
        assert plugin.commands.search_command(host, "") == "Nothing changed." and len(host.asked) == 2  # the screen, left, is the end: the hub is not asked again
    host = Host(["providers", "provider:exa", None])  # and so is a provider's modes
    assert plugin.commands.search_command(host, "") == "Nothing changed." and len(host.asked) == 3
    assert not _puts(signed_in.stub)


def test_when_nothing_can_ask_the_hub_gives_way_to_the_listing_and_saves_nothing(plugin, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder optimized (`/search optimized`, the default): Grounder picks the provider for your question.\nGrounder preset (`/search <preset>`):\n")
    assert "  exa (Exa): instant, deep" in reply and "  tavily (Tavily)\n" in reply
    assert not (tmp_path / "config.yaml").exists()


def test_search_apis_opens_the_live_apis_screen_alone_and_ends_on_done_with_the_settings_in_words(plugin, lists, signed_in, tmp_path):
    host = Host(["polymarket", "off", "espn", "always", "back"])
    assert plugin.commands.search_command(host, "apis") == _apis_reply(every=["ESPN"], never=["Polymarket"])
    assert [question for question, _ in host.asked].count("How should Grounder search?") == 0  # typed, it skips the hub
    assert _labels(host.asked[0][1])[-1] == "Done" and _saved(lists, "live_data_off") == ["polymarket"] and _saved(lists, "live_data_always") == ["espn"]


def test_with_a_search_key_the_lists_are_read_with_no_credential_and_never_through_hermes(plugin, keyed, hermes, lists, tmp_path):
    assert plugin.commands.search_command(Host(), "api:polymarket") == _apis_reply(every=["Polymarket"])
    assert plugin.commands.search_command(Host(), "fast") == _preset_reply(*FAST, apis="1 on Grounder decides, 1 on Every call")  # a preset leaves the live-data APIs alone
    assert hermes.calls == [] and _lists_read(lists) == ["/v1/providers", "/v1/search-profiles"] * 2
    assert all("Authorization" not in r["headers"] for r in lists.requests if r["path"] != "/v1/search/settings")  # the public lists go with no credential
    assert all(r["headers"]["Authorization"] == "Bearer user-key" for r in lists.requests if r["path"] == "/v1/search/settings")  # the account's setting goes with the key
    assert _saved(lists, "server") is None and _saved(lists, "preset") == "fast"


# --- /search with an argument ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("arg,saved,words", [
    ("exa", {"server": "provider:exa", "server_mode": None, "preset": None}, _direct_reply("Exa", *EXA_DEFAULT)),
    ("EXA", {"server": "provider:exa", "server_mode": None, "preset": None}, _direct_reply("Exa", *EXA_DEFAULT)),
    ("provider:tavily", {"server": "provider:tavily", "server_mode": None, "preset": None}, _direct_reply("Tavily")),  # no modes, so no mode line
    ("exa:instant", {"server": "provider:exa", "server_mode": "instant", "preset": None}, _direct_reply("Exa", *EXA_INSTANT)),
    ("mode:exa:deep", {"server": "provider:exa", "server_mode": "deep", "preset": None}, _direct_reply("Exa", *EXA_DEEP)),
    ("fast", {"server": None, "server_mode": None, "preset": "fast"}, _preset_reply(*FAST)),
    ("Fast", {"server": None, "server_mode": None, "preset": "fast"}, _preset_reply(*FAST)),
    ("preset:deep", {"server": None, "server_mode": None, "preset": "deep"}, _preset_reply("Deep", "Thorough research", "1 provider and 1 mode")),
    ("mode:fast", {"server": None, "server_mode": None, "preset": "fast"}, _preset_reply(*FAST)),  # what 0.5 called a preset
    ("cheap", {"server": None, "server_mode": None, "preset": "cheap"}, _preset_reply("Cheap", "Lowest cost")),  # Grounder does not say what it draws from
    ("polymarket", {"server": None, "server_mode": None, "preset": None, "live_data_always": ["polymarket"]}, _apis_reply(every=["Polymarket"])),
    ("api:polymarket", {"server": None, "server_mode": None, "preset": None, "live_data_always": ["polymarket"]}, _apis_reply(every=["Polymarket"])),
])
def test_search_with_an_argument_skips_the_questions(plugin, lists, signed_in, tmp_path, arg, saved, words):
    host = Host()
    assert plugin.commands.search_command(host, arg) == words
    assert host.asked == [] and {key: _saved(lists, key) for key in saved} == saved


def test_a_provider_pinned_after_a_preset_clears_it_and_a_preset_after_a_provider_clears_that(plugin, lists, signed_in, tmp_path):
    _account(lists, **{"preset": "deep"})
    plugin.commands.search_command(Host(), "exa:instant")
    assert (_saved(lists, "preset"), _saved(lists, "server"), _saved(lists, "server_mode")) == (None, "provider:exa", "instant")
    plugin.commands.search_command(Host(), "cheap")
    assert (_saved(lists, "preset"), _saved(lists, "server"), _saved(lists, "server_mode")) == ("cheap", None, None)


@pytest.mark.parametrize("word", ["auto", "optimized", "OPTIMIZED"])
def test_search_optimized_or_auto_clears_the_pick_and_keeps_the_rest(plugin, lists, signed_in, tmp_path, word):
    _config(tmp_path, search_backend="grounder")
    _account(lists, server="provider:exa", server_mode="deep", live_data_off=["espn"])
    host = Host()
    assert plugin.commands.search_command(host, word) == _optimized_reply(apis="1 on Grounder decides, 1 on Never")
    assert host.asked == []
    assert (_saved(lists, "server"), _saved(lists, "server_mode"), _saved(lists, "preset")) == (None, None, None)
    assert _saved(lists, "live_data_off") == ["espn"]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["web"]["search_backend"] == "grounder"


def test_search_with_an_unknown_argument_or_the_default_search_alias_lists_what_there_is_and_saves_nothing(plugin, lists, signed_in, tmp_path):
    for arg in ("nope", "parallel:fast"):
        reply = plugin.commands.search_command(Host(), arg)
        assert reply.splitlines()[0] == f"Grounder has no preset, provider or mode called {arg}."
        assert "  exa (Exa): instant, deep" in reply and "  polymarket (Polymarket)" in reply and "  fast (Fast): Quick answers" in reply and "`/search optimized`" in reply
        assert _saved(lists, "server") is None and _saved(lists, "preset") is None


def test_an_id_that_is_more_than_one_kind_of_thing_needs_its_prefix(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "x", "display_name": "X search"}], "live_data_apis": [{"id": "x", "display_name": "X posts"}]}
    reply = plugin.commands.search_command(Host(), "x")
    assert reply.splitlines()[0] == "x is more than one thing: use `/search provider:x` or `/search api:x`." and _saved(lists, "server") is None
    assert plugin.commands.search_command(Host(), "api:x") == _apis_reply(every=["X posts"])
    assert _saved(lists, "server") is None and _saved(lists, "live_data_always") == ["x"]  # a pick the live-data API setting carries, not a server


def test_when_nothing_can_ask_search_lists_what_there_is(plugin, lists, signed_in, tmp_path):
    reply = plugin.commands.search_command(Host(), "")
    assert reply.startswith("Grounder optimized (`/search optimized`, the default): Grounder picks the provider for your question.\n"
                            "Grounder preset (`/search <preset>`):\n  fast (Fast): Quick answers\n")
    assert ("Grounder pinned (`/search <provider>` or `/search <provider>:<mode>`, the only search provider called), each provider with its modes:\n"
            "  exa (Exa): instant, deep\n  tavily (Tavily)\n  parallel (Parallel): turbo\n") in reply
    assert ("Live-data APIs, each Every call, Never or Grounder decides (`/search apis <id>=every|never|auto`), in every mode:\n"
            "  polymarket (Polymarket): Grounder decides\n  espn (ESPN): Grounder decides\n") in reply and "`/search <id>`" in reply
    assert reply.splitlines()[-1] == ("Use `/search <id>` to choose a Grounder preset or the one search provider to call (Grounder pinned; `/search <provider>:<mode>` for one of its modes), "
                                      "`/search optimized` to let Grounder pick the provider, `/search direct` for Grounder direct by itself, `/search addon on|off` for it as an add-on, or `/search apis` to set each live-data API to Every call, Never or Grounder decides.")
    assert _saved(lists, "server") is None


# --- every row one line (ROW_WIDTH) ------------------------------------------------------------------------------------

def test_every_row_of_every_card_is_one_line_whatever_is_set_and_however_long_a_name_is(plugin, signed_in, lists):
    long = "A provider with a very long name indeed, longer than a row"
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"{long} {n}" if n % 2 else f"P{n}", "description": "It searches a great many things, at length, and says so in a long line.",
                                           "offerings": [{"type": f"p{n}", "mode": None, "features": ["web-search", "page-text", "academic-papers", "deep-research"]},
                                                         {"type": f"p{n}:a_mode_with_a_long_name", "mode": "a_mode_with_a_long_name",
                                                          "description": "A mode whose own line is far longer than any row could ever hold on one line."}]} for n in range(22)],
                            "live_data_apis": [{"id": f"a{n}", "display_name": f"A live-data API with a long name {n}"} for n in range(25)]}
    lists.profiles_body = {"profiles": [{"id": f"s{n}", "name": f"A preset with a long name {n}", "description": "It answers in a particular way, and this sentence says how at some length.",
                                         "includes": ["p1", "p2:a_mode_with_a_long_name"]} for n in range(8)]}
    assert plugin.commands.ROW_WIDTH == 56  # a numbered row is one line in the classic CLI's panel from a 66-column terminal up
    for saved in ({}, {"preset": "s3"}, {"server": "provider:p1", "server_mode": "a_mode_with_a_long_name"},
                  {"server": "provider:p0", "live_data_off": ["a1"], "live_data_always": ["a2", "a3"]}):
        _account(lists, **saved)
        host = Host(["presets", "back", "providers", "provider:p1", "back", "back", "apis", "a4", "back", "back", "done"])
        assert plugin.commands.search_command(host, "") == "Nothing changed." and len(host.asked) == 11  # the hub, and every screen it leads to
        for question, choices in host.asked:
            for n, choice in enumerate(choices, 1):
                assert len(_row_text(n, choice)) <= 56, (saved, question, _row_text(n, choice))
        if saved.get("server_mode"):  # a name too long for its row is cut before the mark, which stays whole
            assert host.asked[0][1][2]["label"].endswith("… (current)") and host.asked[4][1][1]["label"] == "a_mode_with_a_long_name (current)"
    assert len(host.asked[3][1]) == 23 and len(host.asked[7][1]) == 27  # all 22 providers and all 25 APIs, each list one card


def test_a_row_says_the_longest_note_that_fits_then_one_cut_at_a_word_then_none(plugin):
    row = lambda **given: plugin.commands._card([{"value": "v", **given}])[0]
    assert row(label="Short", notes=("n" * 60, "this one fits", "so")) == {"value": "v", "label": "Short", "description": "this one fits"}
    cut = row(label="Short", notes=("one two three four five six seven eight nine ten eleven twelve thirteen",))
    assert cut["description"].endswith("…") and " " in cut["description"] and not cut["description"].endswith(" …") and len(_row_text(1, cut)) <= 56
    assert row(label="L" * 44, notes=("a note with no room left for it",)) == {"value": "v", "label": "L" * 44}  # a few characters of a note say nothing
    assert row(label="Plain") == {"value": "v", "label": "Plain"} and row(label="Plain", notes=("",)) == {"value": "v", "label": "Plain"}
    marked = row(label="N" * 70, tail=" (current)")
    assert marked["label"].endswith("… (current)") and len(_row_text(1, marked)) <= 56
    tenth = plugin.commands._card([{"value": str(n), "label": "L" * 60} for n in range(10)])
    assert [len(_row_text(n, choice)) for n, choice in enumerate(tenth, 1)] == [56] * 10  # a two-digit number leaves a row one character less


def test_the_hubs_live_data_row_counts_in_fewer_words_when_the_full_ones_do_not_fit(plugin):
    counts = plugin.commands._apis_counts
    states = {**{f"a{n}": "auto" for n in range(23)}, "x": "always", "y": "off"}
    assert counts(states) == "23 on Grounder decides, 1 on Every call, 1 on Never" and counts(states, brief=True) == "23 auto, 1 every call, 1 never"
    assert plugin.commands._card([{"value": "apis", "label": "Live-data APIs", "notes": (counts(states), counts(states, brief=True))}]) == [
        {"value": "apis", "label": "Live-data APIs", "description": "23 auto, 1 every call, 1 never"}]


def test_a_providers_row_says_what_its_standard_search_returns_and_how_many_modes_it_has(plugin, signed_in, lists):
    lists.providers_body = {"providers": [
        {"provider": "one", "display_name": "One", "offerings": [{"type": "one", "mode": None, "features": ["web-search", "page-text"]}, {"type": "one:m", "mode": "m"}]},
        {"provider": "two", "display_name": "Two", "offerings": [{"type": "two", "mode": None, "features": ["encyclopedia"]}]},
        {"provider": "three", "display_name": "Three", "offerings": [{"type": "three", "mode": None}, {"type": "three:m", "mode": "m"}, {"type": "three:n", "mode": "n"}]}], "live_data_apis": []}
    host = Host(["providers", "back", "done"])
    plugin.commands.search_command(host, "")
    assert [c.get("description") for c in host.asked[1][1][:3]] == ["web search, page text · 1 mode", "encyclopedia", "2 modes"]


def test_the_reply_to_a_pick_names_the_provider_its_mode_what_it_returns_what_narrows_it_and_where_it_is_saved(plugin, signed_in, lists):
    lists.providers_body = {"providers": [{"provider": "one", "display_name": "One", "description": "One's index.", "offerings": [
        {"type": "one", "mode": None, "description": "Its standard search.", "features": ["web-search"], "inputs": ["query", "days", "domains", "urls"]},
        {"type": "one:quick", "mode": "quick", "description": "Cache only.", "features": ["web-search", "page-text"], "inputs": ["query", "language"]}]}],
        "live_data_apis": PROVIDERS["live_data_apis"]}
    assert plugin.commands.search_command(Host(), "one:quick") == "\n".join([
        "✓ Search switched: Grounder pinned", "  Provider: One, the only search provider called", "  Mode: quick · Cache only", "  Capabilities: web search, page text",
        "  Filters: language", "  Results: only what it finds, even when that is nothing", "  Live-data APIs: 2 on Grounder decides (`/search apis`)",
        "  (saved to your Grounder account; applies wherever it searches)"])
    reply = plugin.commands.search_command(Host(["providers", "provider:one", "provider:one"]), "")  # the menu answers the same block: Default is its standard search
    assert reply.splitlines()[:5] == ["✓ Search switched: Grounder pinned", "  Provider: One, the only search provider called", "  Mode: Default · Its standard search",
                                      "  Capabilities: web search", "  Filters: date window, sites"]  # the query and the pages to read are not filters


def test_no_line_of_a_reply_is_longer_than_a_terminal_shows_whole_and_a_long_one_wraps_at_a_word(plugin, signed_in, lists):
    lists.profiles_body = {"profiles": [{"id": "long", "name": "Long", "includes": ["exa:deep", "tavily"],
                                         "description": "Deep research for a thorough, cited answer: it reads many pages, so it takes longer than one search."}]}
    lists.providers_body = {**PROVIDERS, "live_data_apis": [{"id": f"a{n}", "display_name": f"A{n}"} for n in range(120)]}
    _account(lists, live_data_off=[f"a{n}" for n in range(1, 31)], live_data_always=["a0"])
    reply = plugin.commands.search_command(Host(), "long")
    assert plugin.commands.REPLY_WIDTH == 64 and all(len(line) <= 64 for line in reply.splitlines())
    assert reply.splitlines()[:7] == ["✓ Search switched: Grounder preset", "  Preset: Long", "  For: Deep research for a thorough, cited answer: it reads many",
                                      "    pages, so it takes longer than one search", "  Provider: Grounder picks the best provider for this preset",
                                      "  Draws from: 2 providers and 1 mode", "  Live-data APIs: 89 auto, 1 every call, 30 never"]  # counted in fewer words: the full ones do not fit
    listed = plugin.commands.search_command(Host(), "apis a31=never")
    assert all(len(line) <= 64 for line in listed.splitlines()) and listed.splitlines()[2].startswith("  Never: A1, A2, ") and listed.splitlines()[3].startswith("    A")


# --- a surface that caps a card ----------------------------------------------------------------------------------------

def _many(n, prefix="p"):
    return {"providers": [{"provider": f"{prefix}{i}", "display_name": f"P{i}"} for i in range(n)], "live_data_apis": []}


def test_a_long_list_is_one_view_with_every_row_then_back_and_no_more_row(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    host = Host(["providers", "provider:p5"])
    assert plugin.commands.search_command(host, "") == _direct_reply("P5", apis=None)
    (question, choices), = host.asked[1:2]
    assert question == "Which provider should Grounder pin?"
    assert _labels(choices) == [f"P{i}" for i in range(22)] + ["← Back"]  # all 22 on one card: no More row, and Back leaves the list
    assert _saved(lists, "server") == "provider:p5"
    _account(lists, **{"server": "provider:p19"})
    host = Host(["providers", "back", "done"])
    plugin.commands.search_command(host, "")
    assert _labels(host.asked[1][1])[19] == "P19 (current)" and len(host.asked[1][1]) == 23  # the one set now is marked where it stands


def test_where_a_surface_caps_a_card_a_long_list_is_pages_with_more_and_its_range_and_back_on_a_later_page_goes_to_the_page_before(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    host = Host(["providers", "__more__", "__more__", "__more__", "back", "back", "back", "provider:p5"], surface="whatsapp_cloud")
    assert plugin.commands.search_command(host, "") == _direct_reply("P5", apis=None)
    cards = [(question, choices) for question, choices in host.asked[1:8]]
    assert [len(choices) for _, choices in cards] == [9, 9, 9, 2, 9, 9, 9]  # seven items, More and Back; the last page has 1 item and Back
    first, second, third, fourth = (_labels(choices) for _, choices in cards[:4])
    assert first == [f"P{i}" for i in range(7)] + ["More: 8–14 of 22", "← Back"]
    assert second == [f"P{i}" for i in range(7, 14)] + ["More: 15–21 of 22", "← Back: 1–7 of 22"]
    assert third[-2:] == ["More: 22–22 of 22", "← Back: 8–14 of 22"] and fourth == ["P21", "← Back: 15–21 of 22"]  # no More on the last page
    # Back on page 4 went to page 3, then to page 2, then to page 1; Back on page 1 would have left the list.
    assert [_labels(choices)[0] for _, choices in cards[4:]] == ["P14", "P7", "P0"]
    assert all(question == "Which provider should Grounder pin?" for question, _ in cards)
    assert _saved(lists, "server") == "provider:p5"


def test_back_on_the_first_page_leaves_the_list_for_the_hub(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    host = Host(["providers", "back", "done"])
    assert plugin.commands.search_command(host, "") == "Nothing changed."
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder pin?", "How should Grounder search?"]


def test_a_paged_list_opens_on_the_page_that_holds_the_current_choice_and_marks_it(plugin, signed_in, lists, tmp_path):
    lists.providers_body = _many(22)
    _account(lists, **{"server": "provider:p19"})
    host = Host(["providers", "back", "back", "back", "done"], surface="whatsapp_cloud")
    plugin.commands.search_command(host, "")
    labels = _labels(host.asked[1][1])  # the third page, where p19 is
    assert labels[:7] == [f"P{i}" for i in range(14, 19)] + ["P19 (current)", "P20"] and labels[-2:] == ["More: 22–22 of 22", "← Back: 8–14 of 22"]
    assert [_labels(choices)[0] for _, choices in host.asked[2:4]] == ["P7", "P0"]  # Back on a later page is the page before
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa", "offerings": [{"type": "exa", "mode": None}] + [
        {"type": f"exa:m{i}", "mode": f"m{i}", "description": f"Mode {i}"} for i in range(16)]}], "live_data_apis": []}
    _account(lists, **{"server": "provider:exa", "server_mode": "m12"})
    host = Host(["providers", "provider:exa", "back", "back", "back", "back", "done"], surface="whatsapp_cloud")
    plugin.commands.search_command(host, "")
    modes = _labels(host.asked[2][1])  # six modes a page beside Default, More and Back: m12 is on the third page, so that is where the modes open
    assert "m12 (current)" in modes and modes[0] == "Default" and modes[-1] == "← Back: 7–12 of 16" and not any(label.startswith("More") for label in modes)


def test_a_providers_modes_are_one_view_under_default_with_their_own_descriptions(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa", "description": "Exa the engine", "offerings": [
        {"type": "exa", "mode": None, "description": "Exa's standard search"}] + [{"type": f"exa:m{i}", "mode": f"m{i}", "description": f"Mode {i} line"} for i in range(16)]}],
        "live_data_apis": []}
    host = Host(["providers", "provider:exa", "mode:exa:m10"])
    assert plugin.commands.search_command(host, "") == _direct_reply("Exa", "Mode: m10 · Mode 10 line", apis=None)
    modes = host.asked[2][1]
    assert _labels(modes) == ["Default"] + [f"m{i}" for i in range(16)] + ["← Back"]  # Default first, then all sixteen: one card
    assert [c["description"] for c in modes[:3]] == ["Exa's standard search", "Mode 0 line", "Mode 1 line"]  # the API's own lines, the default's included


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
    host = Host(["providers", "__more__", "__more__", "__more__", "provider:p25"], surface="whatsapp_cloud")
    assert plugin.commands.search_command(host, "") == _direct_reply("P25", apis=None)
    cards = [[c["value"] for c in choices] for _, choices in host.asked[1:5]]
    assert all(len(card) <= 9 for card in cards) and [len(card) for card in cards] == [9, 9, 9, 9]  # seven items, More and Back
    assert cards[0][-2:] == ["__more__", "back"] and "provider:p21" in cards[3] and _saved(lists, "server") == "provider:p25"
    _account(lists, **{})
    host = Host(["providers", "provider:p3"], surface="discord")
    plugin.commands.search_command(host, "")
    assert len(host.asked[1][1]) == 24 and _values(host.asked[1][1])[-2:] == ["__more__", "back"]  # 30 do not fit Discord's 24: twenty-two items, More and Back
    lists.providers_body = _many(22)
    host = Host(["providers", "provider:p3"], surface="discord")
    plugin.commands.search_command(host, "")
    assert len(host.asked[1][1]) == 23 and "__more__" not in _values(host.asked[1][1])  # 22 and Back do: one card, as everywhere a card is not capped


def test_the_live_data_apis_are_one_view_and_where_they_are_paged_every_page_has_set_all_and_back_and_the_list_returns_to_its_page(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": "exa", "display_name": "Exa"}], "live_data_apis": [{"id": f"a{n}", "display_name": f"A{n}"} for n in range(20)]}
    host = Host(["apis", "back", "done"])
    plugin.commands.search_command(host, "")
    assert _values(host.asked[1][1]) == ["__all__"] + [f"a{n}" for n in range(20)] + ["back"]  # Set all, all twenty and Back: one card
    # WhatsApp holds nine: Set all, six APIs, More and Back.
    host = Host(["apis", "__more__", "__more__", "a17", "always", "back", "back", "back"], surface="whatsapp_cloud")
    assert plugin.commands.search_command(host, "") == _apis_reply(every=["A17"])
    cards = [[c["value"] for c in choices] for _, choices in host.asked[1:4]]
    assert [len(card) for card in cards] == [9, 9, 9]
    assert all(card[0] == "__all__" and card[-2:] == ["__more__", "back"] for card in cards)  # Set all first, then More and Back last, on every page but the last
    assert host.asked[5][0] == "Live-data APIs: pick one to change how Grounder uses it." and host.asked[5][1][1]["value"] == "a12"  # asked again on the page it was on
    assert _labels(host.asked[3][1])[-1] == "← Back: 7–12 of 20" and _saved(lists, "live_data_always") == ["a17"] and _saved(lists, "live_data_off") is None
    assert _labels(host.asked[1][1])[-1] == "← Back" and _labels(host.asked[7][1])[-1] == "Done"  # the first page's way out, before a change and after one


# --- the form, where a Hermes offers ctx.ask_form -----------------------------------------------------------------------

def test_a_hermes_with_ask_form_gets_two_pages_the_presets_in_a_row_the_modes_under_their_provider_and_what_each_preset_lights(plugin, lists, signed_in, tmp_path):
    _account(lists, **{"server": "provider:exa", "server_mode": "deep", "live_data_off": ["espn"]})
    host = Host(form={"pick": "preset:fast", "apis": ["polymarket"]})
    plugin.commands.search_command(host, "")
    assert host.asked == []
    pick, apis = host.pages
    assert (pick["id"], pick["tab"], pick["optional"], pick["selected"]) == ("pick", "Search", True, "mode:exa:deep")  # it starts where the saved choice is
    values = [c["value"] for c in pick["choices"] if "value" in c]
    assert values == ["auto", "preset:fast", "preset:cheap", "preset:live", "preset:deep", "preset:code", "preset:balanced",
                      "provider:exa", "mode:exa:instant", "mode:exa:deep", "provider:tavily", "provider:parallel", "mode:parallel:turbo"]
    # The three search modes in order, each under its header: Grounder optimized (one row), Grounder preset (a row of presets), Grounder pinned (the providers).
    assert pick["choices"][:3] == [{"header": "Grounder optimized"},
                                   {"value": "auto", "label": "Grounder picks the provider for your question",
                                    "description": "The default. The provider's own results, each uncut and in its own order."},
                                   {"header": "Grounder preset", "row": True}]
    headers = [c["header"] for c in pick["choices"] if "header" in c]
    assert headers == ["Grounder optimized", "Grounder preset", "Grounder pinned"] and {"header": "Grounder pinned"} in pick["choices"]
    assert pick["choices"].index({"header": "Grounder pinned"}) == 3 + 6  # after the six presets
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


def test_what_the_form_answers_is_saved_each_live_api_by_its_one_setting(plugin, lists, signed_in, tmp_path):
    host = Host(form={"pick": "mode:exa:deep", "apis": ["polymarket:auto", "espn:off"]})
    assert plugin.commands.search_command(host, "") == _direct_reply("Exa", *EXA_DEEP, apis="1 on Grounder decides, 1 on Never") + "\n" + _apis_reply(never=["ESPN"])
    assert (_saved(lists, "server"), _saved(lists, "server_mode"), _saved(lists, "preset"), _saved(lists, "live_data_off")) == ("provider:exa", "deep", None, ["espn"])
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:always", "espn:auto"]}), "") == (
        _optimized_reply(apis="1 on Grounder decides, 1 on Every call") + "\n" + _apis_reply(every=["Polymarket"]))
    assert _saved(lists, "server") is None and _saved(lists, "live_data_off") is None  # nothing set to Never: the key is not kept
    assert _saved(lists, "live_data_always") == ["polymarket"]
    # An API with none of its settings checked, or two of them, is left as it is (a form that did not hold the one-per-row rule).
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:always", "polymarket:off", "espn:never"]}), "") == "Nothing changed."
    assert _saved(lists, "live_data_always") == ["polymarket"] and _saved(lists, "live_data_off") is None


def test_a_form_that_changes_only_the_live_data_page_leaves_the_pick_it_cannot_show_alone(plugin, lists, signed_in, tmp_path):
    for saved in ({"server": "provider:defunct", "server_mode": "x"}, {"preset": "gone"}):
        _account(lists, **saved)
        host = Host(form={"pick": "auto", "apis": ["polymarket:auto", "espn:off"]})  # page one answers as it started: on Grounder optimized, the nearest row
        assert plugin.commands.search_command(host, "") == _apis_reply(never=["ESPN"])
        assert {k: _saved(lists, k) for k in saved} == saved and _saved(lists, "live_data_off") == ["espn"]
        assert _puts(lists)[-1] == {"specialized": {"espn": {"use": "off"}}}  # only the live-data page was written: the pick it could not show is not sent
    _account(lists, **{"preset": "fast"})
    assert plugin.commands.search_command(Host(form={"pick": "preset:fast", "apis": ["polymarket:auto", "espn:auto"]}), "") == "Nothing changed."
    assert plugin.commands.search_command(Host(form={"pick": "auto", "apis": ["polymarket:auto", "espn:auto"]}), "") == _optimized_reply()


def test_a_form_of_one_page_has_no_tab_hint(plugin, signed_in, lists, tmp_path):
    only = Host(form={"apis": ["espn:auto", "polymarket:auto"]})
    plugin.commands.search_command(only, "apis")
    assert "Tab" not in only.pages[0]["hint"] and only.pages[0]["hint"] == "↑/↓ move · ←/→ + Space sets · type to filter · Enter confirms"
    lists.providers_body = {**PROVIDERS, "live_data_apis": []}
    host = Host(form={"pick": "auto"})
    plugin.commands.search_command(host, "")
    assert [p["id"] for p in host.pages] == ["pick"] and "Tab" not in host.pages[0]["hint"]  # with no live-data APIs listed there is no second page


def test_an_api_that_is_new_to_grounder_is_grounder_decides_in_the_form_until_the_user_sets_it(plugin, signed_in, lists, tmp_path):
    _account(lists, **{"live_data_off": ["espn"]})
    lists.providers_body = {**PROVIDERS, "live_data_apis": [*PROVIDERS["live_data_apis"], {"id": "newcomer", "display_name": "Newcomer"}]}
    host = Host(form=None)
    plugin.commands.search_command(host, "")
    assert host.pages[1]["checked"] == ["polymarket:auto", "espn:off", "newcomer:auto"]


def test_a_cancelled_form_changes_nothing_and_search_apis_is_the_second_page_alone(plugin, lists, signed_in, tmp_path):
    assert plugin.commands.search_command(Host(form=None), "") == "Nothing changed." and not (tmp_path / "config.yaml").exists()
    host = Host(form={"apis": ["espn:always", "polymarket:off"]})
    assert plugin.commands.search_command(host, "apis") == _apis_reply(every=["ESPN"], never=["Polymarket"])
    assert [page["id"] for page in host.pages] == ["apis"] and host.pages[0]["optional"] is False
    assert _saved(lists, "live_data_off") == ["polymarket"] and _saved(lists, "live_data_always") == ["espn"]


def test_a_hermes_whose_form_is_not_available_here_asks_in_the_hub(plugin, signed_in, tmp_path):
    host = Host(["presets", "preset:fast"], form=None)
    host.supports_form = lambda: False
    assert plugin.commands.search_command(host, "") == _preset_reply(*FAST) and host.pages is None and len(host.asked) == 3


# --- the lists come from the API, when asked -------------------------------------------------------------------------------

def test_the_lists_are_read_each_time_so_a_new_provider_or_preset_is_on_offer_at_once(plugin, signed_in, lists):
    plugin.commands.search_command(Host(["auto", "direct-no"]), "")
    lists.providers_body = {"providers": [{"provider": "newcomer", "display_name": "Newcomer"}], "live_data_apis": []}
    lists.profiles_body = {"profiles": [{"id": "later", "name": "Later", "description": "Added since"}]}
    second, third = Host(["providers", "provider:newcomer", "direct-no"]), Host(["presets", "preset:later", "direct-no"])
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
    assert _saved(lists, "server") is None


def test_a_sign_in_hermes_refuses_is_said_not_raised(plugin, hermes, lists):
    hermes.raises = plugin.provider.AuthError("grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set.", provider="grounder:grounder", code="oauth_client_id_missing")
    assert plugin.commands.search_command(Host(), "") == "Could not load Grounder's list of providers: Grounder sign-in failed: grounder:grounder: GROUNDER_OAUTH_CLIENT_ID is not set."


def test_on_slack_the_hub_runs_with_no_form_and_a_long_list_is_one_card(plugin, signed_in, lists, tmp_path):
    lists.providers_body = {"providers": [{"provider": f"p{n}", "display_name": f"P{n}"} for n in range(22)], "live_data_apis": []}
    host = Host(["providers", "provider:p21"], surface="slack")  # `/hermes search`: Slack's own /search is not this command
    assert plugin.commands.search_command(host, "") == _direct_reply("P21", apis=None)
    assert [len(choices) for _, choices in host.asked] == [6, 23, 2]
    assert _saved(lists, "server") == "provider:p21"


# --- what a search then sends ---------------------------------------------------------------------------------------------

def _sent(stub):
    return json.loads([r for r in stub.requests if r["path"] == "/v1/search"][-1]["body"])


def _searches(stub):
    return [json.loads(r["body"]) for r in stub.requests if r["path"] == "/v1/search"]


# --- a choice Grounder no longer offers ------------------------------------------------------------------------------------

def _refused(message):
    return 400, {"error": {"code": "invalid_request", "message": message}}


_OK = (200, {"query": "q", "results": [{"title": "T", "url": "https://example.com/a", "text": "x", "source": "exa"}]})


def test_the_words_everywhere_say_only_that_provider_and_that_what_it_finds_is_what_you_get_even_when_that_is_nothing(plugin, signed_in, lists):
    assert plugin.commands.PIN_NOTE == "The only search provider called."
    reply = plugin.commands.search_command(Host(), "exa:instant")
    assert "Provider: Exa, the only search provider called" in reply and "Results: only what it finds, even when that is nothing" in reply
    docs = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md")}
    docs["plugin.yaml"] = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name, text in docs.items():
        assert "to always include" not in text and "always included in a search" not in text, name
        assert "even when that is nothing" in text and "Grounder picks for you" not in text, name
    source = "\n".join((PLUGIN_DIR / name).read_text() for name in ("commands.py", "provider.py"))
    assert "Grounder will always include" not in source and "will search with only" not in source


# --- Grounder's three search modes: optimized, preset, pinned --------------------------------------------------------------------

def test_the_three_modes_are_named_by_the_constants_the_hermes_copy_uses(plugin):
    commands = plugin.commands
    assert (commands.OPTIMIZED_HEADER, commands.PRESETS_HEADER, commands.PROVIDERS_HEADER) == ("Grounder optimized", "Grounder preset", "Grounder pinned")
    assert commands.AUTO_LABEL == "Grounder picks the provider for your question"
    assert commands.AUTO_NOTE == "The default. The provider's own results, each uncut and in its own order."
    assert commands.PRESET_NOTE == "Grounder picks the best provider for this preset."
    assert commands.PIN_NOTE == "The only search provider called."


def test_every_document_and_docstring_names_the_three_modes_and_none_says_the_old_words(plugin):
    texts = {name: (PLUGIN_DIR / name).read_text() for name in ("README.md", "after-install.md", "commands.py", "provider.py", "__init__.py")}
    texts["plugin.yaml"] = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["description"]
    for name, text in texts.items():
        for mode in ("Grounder optimized", "Grounder preset", "Grounder pinned"):
            assert mode in text, (name, mode)
        assert "based on your prompt" not in text and "Let Grounder pick for you" not in text and "let Grounder pick for you" not in text, name
    for name in ("README.md", "after-install.md", "plugin.yaml"):
        assert "uncut" in texts[name] and "reranked" in texts[name], name  # every mode returns what the provider returns
    readme = texts["README.md"]
    assert all(f"| **{mode}** |" in readme for mode in ("Grounder optimized", "Grounder preset", "Grounder pinned"))  # the table of the three search modes
    for stale in ("800 characters", "6,000", "30,000", "20 rows", "two notes", "saves no page to a file"):
        assert not any(stale in text for name, text in texts.items() if name in ("README.md", "after-install.md", "plugin.yaml")), stale


def test_the_screens_that_ask_for_a_provider_and_a_mode_say_which_mode_they_are_choosing(plugin, signed_in, tmp_path):
    host = Host(["providers", "provider:exa", "mode:exa:deep"])
    assert plugin.commands.search_command(host, "").splitlines()[0] == "✓ Search switched: Grounder pinned"
    assert [question for question, _ in host.asked] == ["How should Grounder search?", "Which provider should Grounder pin?", "Which Exa mode?", "Also read sites you name directly, as you?"]
    again = Host(["done"])
    plugin.commands.search_command(again, "")
    assert host.asked[0][1][2]["label"] == "Grounder pinned" and again.asked[0][1][2]["label"] == "Grounder pinned: Exa deep (current)"


def test_a_picks_words_still_say_grounder_direct_for_a_mode_or_provider_grounder_no_longer_lists(plugin, signed_in):
    catalog, _ = plugin.commands._catalog()
    saved = plugin.provider.read_account()[0]
    lines = lambda value: plugin.commands._switched(saved, catalog, value).splitlines()
    assert lines("mode:exa:gone")[:3] == ["✓ Search switched: Grounder pinned", "  Provider: exa:gone, the only search provider called",
                                          "  Results: only what it finds, even when that is nothing"]
    assert lines("provider:gone")[:2] == ["✓ Search switched: Grounder pinned", "  Provider: gone, the only search provider called"]
    assert lines("preset:gone")[:3] == ["✓ Search switched: Grounder preset", "  Preset: gone", "  Provider: Grounder picks the best provider for this preset"]


# --- a live-data API: Every call, Never or Grounder decides, in every mode -------------------------------------------------------

_MODES = {"optimized": {}, "preset": {"preset": "fast"}, "direct": {"server": "provider:exa", "server_mode": "deep"}}
_SETTINGS = {"always": {"use": "always"}, "off": {"use": "off"}, "auto": None}


def test_search_apis_with_arguments_sets_apis_without_the_page_and_saves_every_pair_or_none(plugin, lists, signed_in, tmp_path):
    host = Host()
    assert plugin.commands.search_command(host, "apis espn=every polymarket=never") == _apis_reply(every=["ESPN"], never=["Polymarket"])
    assert _saved(lists, "live_data_always") == ["espn"] and _saved(lists, "live_data_off") == ["polymarket"] and host.asked == []
    assert plugin.commands.search_command(host, "apis ESPN:auto") == _apis_reply(never=["Polymarket"])  # a name, a colon, any case
    assert _saved(lists, "live_data_always") is None
    before = (_saved(lists, "live_data_always"), _saved(lists, "live_data_off"))
    reply = plugin.commands.search_command(host, "apis espn=every nonesuch=never")
    assert reply.startswith("Grounder has no live-data API called nonesuch.\n") and "  espn (ESPN): Grounder decides" in reply
    reply = plugin.commands.search_command(host, "apis espn=sometimes")
    assert reply == "`espn=sometimes` is not a setting: use `espn=every`, `espn=never` or `espn=auto` (Grounder decides)."
    assert (_saved(lists, "live_data_always"), _saved(lists, "live_data_off")) == before  # a pair that is wrong saves none of them
    for word, state in (("every", "always"), ("always", "always"), ("never", "off"), ("off", "off"), ("decides", "auto"), ("auto", "auto")):
        plugin.commands.search_command(host, f"apis espn={word}")
        assert plugin.commands._live_state(plugin.provider.read_account()[0], "espn") == state, word


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


# --- the account's setting: read when /search opens, written with every choice, shared with grounder.net's Routing page ----------------

def test_the_hub_reads_the_account_once_when_it_opens_and_writes_each_choice_as_one_write_answered_by_that_write(plugin, signed_in, lists, tmp_path):
    host = Host(["apis", "espn", "always", "polymarket", "off", "back"])
    assert plugin.commands.search_command(host, "") == _apis_reply(every=["ESPN"], never=["Polymarket"])
    assert len(_reads(lists)) == 1  # the screens after the first use what each write answered, so no turn reads the account again
    assert _puts(lists) == [{"specialized": {"espn": {"use": "always"}}}, {"specialized": {"polymarket": {"use": "off"}}}]
    host = Host(["providers", "provider:exa", "mode:exa:instant"])  # a pick ends the menu, so it is the one write of its run
    plugin.commands.search_command(host, "")
    assert len(_reads(lists)) == 2 and _puts(lists)[2:] == [{"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "instant", "fallback": False}}]
    # What the site's Routing page reads now is what was chosen here, and nothing was kept in Hermes's own config.
    assert lists.saved_search == {"mode": "direct", "profile": None, "requested_provider": "exa", "provider_mode": "instant", "fallback": False}
    assert lists.saved_live == {"espn": "always", "polymarket": "off"}
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.parametrize("typed,body", [
    ("auto", {"search": {"mode": "optimized"}}),
    ("OPTIMIZED", {"search": {"mode": "optimized"}}),
    ("fast", {"search": {"mode": "preset", "profile": "fast"}}),
    ("exa", {"search": {"mode": "direct", "requested_provider": "exa", "fallback": False}}),
    ("tavily", {"search": {"mode": "direct", "requested_provider": "tavily", "fallback": False}}),
    ("exa:instant", {"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "instant", "fallback": False}}),
    ("mode:exa:deep", {"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "deep", "fallback": False}}),
    ("api:polymarket", {"specialized": {"polymarket": {"use": "always"}}}),
    ("polymarket", {"specialized": {"polymarket": {"use": "always"}}}),
    ("apis espn=every polymarket=never", {"specialized": {"espn": {"use": "always"}, "polymarket": {"use": "off"}}}),
])
def test_every_typed_choice_is_one_write_to_the_account(plugin, signed_in, lists, typed, body):
    _account(lists, preset="deep", live_data_off=["espn"])
    host = Host()
    plugin.commands.search_command(host, typed)
    assert host.asked == [] and len(_reads(lists)) == 1
    # A live-data API's setting is written only where it differs from what the account showed (espn was Never, so `espn=every` is a change and nothing else is).
    assert _puts(lists) == [body]
    assert lists.saved_search["mode"] == (body["search"]["mode"] if "search" in body else "preset")  # a live-data change leaves the mode alone


def test_every_live_data_setting_is_written_by_the_hub_and_set_all_returns_each_to_grounder_decides(plugin, signed_in, lists):
    _account(lists, live_data_off=["polymarket"], live_data_always=["espn"])
    host = Host(["apis", "polymarket", "auto", "espn", "off", "polymarket", "always", "__all__", "back", "done"])
    plugin.commands.search_command(host, "")
    assert _puts(lists) == [{"specialized": {"polymarket": {"use": "auto"}}}, {"specialized": {"espn": {"use": "off"}}}, {"specialized": {"polymarket": {"use": "always"}}},
                            {"specialized": {"polymarket": {"use": "auto"}, "espn": {"use": "auto"}}}]
    assert lists.saved_live == {}


def test_a_live_data_change_that_differs_in_nothing_sends_nothing_and_a_pick_the_account_already_shows_is_still_written(plugin, signed_in, lists):
    plugin.commands.search_command(Host(), "apis espn=auto")
    assert _puts(lists) == []
    plugin.commands.search_command(Host(), "auto")  # the view this command read may be a minute old: the user's pick is written, not skipped
    assert _puts(lists) == [{"search": {"mode": "optimized"}}]


def test_a_change_made_on_the_site_is_what_the_hub_shows_the_next_time_it_opens_and_the_reverse(plugin, signed_in, lists):
    first = Host(["done"])
    plugin.commands.search_command(first, "")
    assert _labels(first.asked[0][1])[:3] == ["Grounder optimized (current)", "Grounder presets", "Grounder pinned"] and first.asked[0][1][4]["description"] == "2 on Grounder decides"
    _account(lists, preset="deep", live_data_off=["espn"])  # the Routing page saved these
    second = Host(["done"])
    assert plugin.commands.search_command(second, "") == "Nothing changed."
    rows = second.asked[0][1]
    assert _labels(rows)[:3] == ["Grounder optimized", "Grounder presets: Deep (current)", "Grounder pinned"] and rows[4]["description"] == "1 on Grounder decides, 1 on Never"
    assert rows[1]["description"] == "6 presets" and rows[2]["description"] == "Exa, Tavily and Parallel"  # the row for what is set counts; the others name
    _account(lists, server="provider:exa", server_mode="instant")
    third = Host(["done"])
    plugin.commands.search_command(third, "")
    assert _labels(third.asked[0][1])[2] == "Grounder pinned: Exa instant (current)"
    plugin.commands.search_command(Host(), "cheap")  # and the other way: what the command saved is what the page reads
    assert lists.saved_search["mode"] == "preset" and lists.saved_search["profile"] == "cheap"


def test_a_form_writes_its_pick_and_its_live_data_page_in_one_write(plugin, signed_in, lists):
    host = Host(form={"pick": "mode:exa:deep", "apis": ["polymarket:auto", "espn:off"]})
    plugin.commands.search_command(host, "")
    assert _puts(lists) == [{"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "deep", "fallback": False}, "specialized": {"espn": {"use": "off"}}}]
    assert len(_reads(lists)) == 1


def test_a_direct_pick_saves_its_fallback_off_and_a_fallback_the_account_saved_for_the_same_provider_stays_on(plugin, signed_in, lists):
    assert plugin.commands.search_command(Host(), "exa") == _direct_reply("Exa", *EXA_DEFAULT)
    assert _puts(lists)[-1]["search"]["fallback"] is False
    _account(lists, server="provider:exa", fallback=True)  # saved on the Routing page: when Exa finds nothing, Grounder picks one other
    reply = plugin.commands.search_command(Host(), "exa:deep")
    assert _puts(lists)[-1]["search"] == {"mode": "direct", "requested_provider": "exa", "provider_mode": "deep", "fallback": True}
    assert reply == _direct_reply("Exa", *EXA_DEEP, fallback=True)
    plugin.commands.search_command(Host(), "tavily")  # another provider is a new choice: the fallback is off
    assert _puts(lists)[-1]["search"]["fallback"] is False and lists.saved_search["fallback"] is False
    _account(lists, server="provider:exa", fallback=True)
    host = Host(["providers", "provider:exa", "provider:exa"])  # the menu says so too, and that the pick was what the account had
    assert plugin.commands.search_command(host, "") == _direct_reply("Exa", *EXA_DEFAULT, fallback=True, changed=False)


def test_a_live_data_api_the_account_saved_and_grounders_list_does_not_show_is_left_as_it_is(plugin, signed_in, lists):
    lists.saved_live = {"bluesky": "always"}
    plugin.commands.search_command(Host(), "apis espn=never")
    assert _puts(lists) == [{"specialized": {"espn": {"use": "off"}}}] and lists.saved_live == {"bluesky": "always", "espn": "off"}
    plugin.commands.search_command(Host(["apis", "__all__", "back", "done"]), "")  # Set all returns the listed ones only
    assert _puts(lists)[-1] == {"specialized": {"espn": {"use": "auto"}}} and lists.saved_live == {"bluesky": "always"}


@pytest.mark.parametrize("arg", ["", "exa", "apis espn=never"])
@pytest.mark.parametrize("status,body,words", [
    (503, {"error": {"code": "unavailable", "message": "later"}}, "Grounder returned HTTP 503: later"),
    (401, {"error": {"code": "unauthorized", "message": "expired"}}, f"Grounder rejected the sign-in. Run `{SIGN_IN}`."),
    (404, {"error": {"code": "not_found", "message": "No such route"}}, "Grounder returned HTTP 404: No such route"),
    (200, {"unexpected": True}, "Grounder's answer was not what was expected"),
    (200, {"search": "optimized"}, "Grounder's answer was not what was expected")])
def test_a_setting_that_cannot_be_read_is_said_with_its_reason_and_nothing_is_asked_or_written(plugin, signed_in, lists, tmp_path, arg, status, body, words):
    lists.settings_script = [(status, body)]
    host = Host(["presets", "preset:fast", "done"])
    assert plugin.commands.search_command(host, arg) == f"Could not read your Grounder search setting: {words}"
    assert host.asked == [] and _puts(lists) == [] and lists.saved_search["mode"] == "optimized" and not (tmp_path / "config.yaml").exists()  # never a quiet local copy


def test_a_write_the_account_refuses_says_grounders_own_words_and_changes_nothing(plugin, signed_in, lists, tmp_path):
    lists.off_providers = {"exa"}
    refusal = "Exa is your search provider and is turned off: turn it on, or choose another provider for your searches first."
    assert plugin.commands.search_command(Host(), "exa") == f"Could not save that to your Grounder account, so nothing changed: {refusal}"
    assert _saved(lists, "server") is None and not (tmp_path / "config.yaml").exists()
    # In the hub the failed write ends the menu with its reason, and what was saved before it stays.
    assert plugin.commands.search_command(Host(["presets", "preset:fast"]), "") == _preset_reply(*FAST)
    host = Host(["providers", "provider:exa", "mode:exa:deep"])
    assert plugin.commands.search_command(host, "") == f"Could not save that to your Grounder account, so nothing changed: {refusal}" and len(host.asked) == 3
    assert _saved(lists, "preset") == "fast" and _saved(lists, "server") is None
    # A failure that is not a 400 says what it is.
    lists.settings_script = [(200, lists.settings_answer()), (503, {"error": {"code": "unavailable", "message": "later"}})]
    assert plugin.commands.search_command(Host(), "fast") == "Could not save that to your Grounder account, so nothing changed: Grounder returned HTTP 503: later"


def test_with_a_key_the_setting_goes_with_the_key_through_the_plugins_own_requests_and_a_failure_is_said(plugin, keyed, hermes, lists, monkeypatch):
    plugin.commands.search_command(Host(), "fast")
    [read] = _reads(lists)
    [write] = [r for r in lists.requests if r["method"] == "PUT"]
    assert read["headers"]["Authorization"] == "Bearer user-key" and write["headers"]["Authorization"] == "Bearer user-key"
    assert write["headers"]["Content-Type"] == "application/json" and write["headers"]["User-Agent"] == plugin.provider.USER_AGENT == read["headers"]["User-Agent"]
    assert json.loads(write["body"]) == {"search": {"mode": "preset", "profile": "fast"}} and hermes.calls == []  # Hermes plays no part in a key's calls
    lists.settings_script = [(302, {})]  # a redirect is never followed, so the key goes nowhere else
    assert plugin.commands.search_command(Host(), "exa") == ("Could not read your Grounder search setting: Grounder answered HTTP 302, a redirect, which this plugin does not follow.")
    monkeypatch.setattr(plugin.provider, "SETTINGS_URL", "http://127.0.0.1:9/v1/search/settings")
    assert plugin.commands.search_command(Host(), "exa").startswith("Could not read your Grounder search setting: Could not reach Grounder")


def test_signed_in_the_setting_goes_through_hermes_which_attaches_the_token_and_the_calls_are_bounded(plugin, signed_in, lists):
    plugin.commands.search_command(Host(), "exa:instant")
    calls = [c for c in signed_in.calls if c["url"].endswith("/v1/search/settings")]
    assert [(c["method"], c["url"]) for c in calls] == [("GET", "https://api.grounder.net/v1/search/settings"), ("PUT", "https://api.grounder.net/v1/search/settings")]
    assert calls[0]["json"] is None and calls[1]["json"] == {"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "instant", "fallback": False}}
    assert not {name.lower() for call in calls for name in call["headers"]} & {"authorization", "cookie"}
    assert {call["timeout"] for call in calls} == {plugin.provider.SETTINGS_TIMEOUT_S} and plugin.provider.SETTINGS_TIMEOUT_S <= 15
    assert all(call["headers"]["User-Agent"] == plugin.provider.USER_AGENT for call in calls)


@pytest.mark.parametrize("answer,local", [
    ({"search": {"mode": "optimized"}}, {"preset": "", "server": "", "server_mode": "", "fallback": False, "off": [], "always": []}),
    ({"search": {"mode": "preset", "profile": "deep"}, "specialized": {"espn": {"use": "off"}, "kalshi": {"use": "always"}, "mlb": {"use": "auto"}}},
     {"preset": "deep", "server": "", "server_mode": "", "fallback": False, "off": ["espn"], "always": ["kalshi"]}),
    ({"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": "instant", "fallback": True}},
     {"preset": "", "server": "provider:exa", "server_mode": "instant", "fallback": True, "off": [], "always": []}),
    ({"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": None, "fallback": False}},
     {"preset": "", "server": "provider:exa", "server_mode": "", "fallback": False, "off": [], "always": []}),
    # Another shape reads as unset: an id that is not one, a field of the wrong type, a mode with no field of its own, entries that are not settings.
    ({"search": {"mode": "preset", "profile": "Not An Id!"}}, {"preset": "", "server": "", "server_mode": "", "fallback": False, "off": [], "always": []}),
    ({"search": {"mode": "direct", "requested_provider": 7, "provider_mode": "x", "fallback": True}}, {"preset": "", "server": "", "server_mode": "", "fallback": False, "off": [], "always": []}),
    ({"search": {"mode": "direct", "requested_provider": "exa", "provider_mode": 7, "fallback": "yes"}}, {"preset": "", "server": "provider:exa", "server_mode": "", "fallback": False, "off": [], "always": []}),
    ({"search": {"mode": "optimized", "fallback": True, "profile": "fast"}, "specialized": {"Bad Id!": {"use": "off"}, "x": "off", "espn": {"use": 3}, "mlb": None}},
     {"preset": "", "server": "", "server_mode": "", "fallback": False, "off": [], "always": []}),
])
def test_the_accounts_answer_is_read_into_what_search_works_with_and_another_shape_reads_as_unset(plugin, answer, local):
    assert plugin.provider._local(answer) == local


def test_a_search_never_reads_or_writes_the_setting(plugin, signed_in, lists):
    _account(lists, preset="deep")
    plugin.GrounderWebSearchProvider().search("q")
    assert [r["path"] for r in lists.requests] == ["/v1/search"]


# --- what a search then sends: none of the saved setting, which Grounder applies -------------------------------------------------

@pytest.mark.parametrize("how", ["keyed", "signed_in"])
@pytest.mark.parametrize("saved", [{}, {"preset": "deep"}, {"server": "provider:exa", "server_mode": "deep"}, {"live_data_off": ["espn"], "live_data_always": ["polymarket"]},
                                   {"server": "provider:tavily", "fallback": True, "live_data_off": ["espn"]}])
def test_a_search_with_a_key_or_a_sign_in_sends_none_of_the_saved_setting_so_grounder_applies_it(plugin, request, lists, how, saved):
    request.getfixturevalue(how)
    _account(lists, **saved)
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search_tool({"query": "q"})
    assert _searches(lists) == [{"query": "q", "top_k": 5}, {"query": "q"}]  # no mode, profile, provider, routing settings or live-data setting of the user's
    assert "Authorization" in [r for r in lists.requests if r["path"] == "/v1/search"][0]["headers"] or how == "signed_in"


@pytest.mark.parametrize("how", ["keyed", "signed_in"])
def test_the_users_saved_choice_wins_over_what_the_model_passes_and_is_not_changed_by_it(plugin, request, lists, how):
    request.getfixturevalue(how)
    _account(lists, server="provider:exa", server_mode="deep", live_data_off=["espn"])
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"specialized": {"espn": {"use": "always"}}, "caps": {"search": {"sources": 5}}}})
    assert _sent(lists) == {"query": "q", "routing_settings": {"caps": {"search": {"sources": 5}}}}  # the user's provider beats the preset, their Never beats Every call; the cap stays
    provider.search_tool({"query": "q", "requested_provider": "tavily", "routing_settings": {"fallback": True}})
    assert _sent(lists) == {"query": "q", "routing_settings": {"fallback": True}}
    pinned = {"providers": {"brave": {"use": "always"}}, "exclusive": True, "fallback": False}
    provider.search_tool({"query": "q", "providers": ["brave"], "routing_settings": pinned})
    assert _sent(lists) == {"query": "q", "routing_settings": {"fallback": False}}  # no pin of the model's: the search runs as the user's provider
    provider.search_tool({"query": "q", "profile": "auto"})
    assert _sent(lists) == {"query": "q"}
    assert lists.saved_search["requested_provider"] == "exa" and lists.saved_live == {"espn": "off"} and not _puts(lists)  # reading it changed nothing


@pytest.mark.parametrize("how", ["keyed", "signed_in"])
def test_what_the_model_passes_goes_through_as_that_searchs_own_choice_when_the_user_chose_none(plugin, request, lists, how):
    request.getfixturevalue(how)
    _account(lists, live_data_off=["espn"])  # Let Grounder pick, one live-data API on Never
    provider = plugin.GrounderWebSearchProvider()
    asked = {"query": "q", "profile": "fast", "routing_settings": {"specialized": {"usgs": {"use": "always"}}, "caps": {"search": {"sources": 5}}}}
    provider.search_tool(asked)
    assert _sent(lists) == asked  # no merging with the saved setting, and no mode named for it
    provider.search_tool({"query": "q", "requested_provider": "tavily", "routing_settings": {"fallback": True}})
    assert _sent(lists) == {"query": "q", "requested_provider": "tavily", "routing_settings": {"fallback": True}}
    provider.search_tool({"query": "q", "profile": "auto"})
    assert _sent(lists) == {"query": "q", "profile": "auto"}  # a search that says no preset says so itself
    assert lists.saved_live == {"espn": "off"} and not _puts(lists)


@pytest.mark.parametrize("how", ["keyed", "signed_in"])
def test_a_page_read_sends_none_of_the_saved_setting_either(plugin, request, lists, how):
    request.getfixturevalue(how)
    _account(lists, preset="deep", live_data_off=["espn"])
    provider = plugin.GrounderWebSearchProvider()
    provider.search_tool({"query": "q", "urls": ["https://example.com/a"]})
    provider.search("https://example.com/a more")
    provider.extract(["https://example.com/a"])
    bodies = _searches(lists)
    assert len(bodies) == 3 and all(body["urls"] == ["https://example.com/a"] for body in bodies)
    assert all(not {"mode", "profile", "routing_settings", "requested_provider"} & set(body) for body in bodies)


def test_a_free_search_is_as_it_was_it_has_no_saved_setting_to_read_and_sends_none_of_the_options_grounder_refuses_it(plugin, free, lists):
    _account(lists, preset="deep", live_data_off=["espn"])
    provider = plugin.GrounderWebSearchProvider()
    provider.search("q")
    provider.search_tool({"query": "q", "profile": "fast", "routing_settings": {"fallback": False}, "top_k": 3})
    assert _searches(lists) == [{"query": "q", "top_k": 5}, {"query": "q", "top_k": 3}]
    assert not [r for r in lists.requests if r["path"] == "/v1/search/settings"] and all("Authorization" not in r["headers"] for r in lists.requests)


def test_a_400_for_a_choice_the_model_passed_is_the_models_to_fix_and_no_search_is_repeated(plugin, signed_in, lists):
    provider = plugin.GrounderWebSearchProvider()
    lists.search_script = [_refused("No such search profile: turbo. GET /v1/search-profiles lists them.")]
    result = json.loads(provider.search_tool({"query": "q", "profile": "turbo"}))
    assert result["success"] is False and "No such search profile: turbo" in result["error"] and len(_searches(lists)) == 1
    lists.search_script = [_refused("No such provider: defunct")]
    assert provider.search("q")["success"] is False and len(_searches(lists)) == 2  # one search each: a second POST would be a second search


def test_the_tool_says_what_the_model_passes_applies_to_that_search_alone_and_names_a_preset(plugin):
    properties = plugin.provider.SEARCH_SCHEMA["parameters"]["properties"]
    assert "applies to this search alone" in properties["routing_settings"]["description"] and "wins over" not in properties["routing_settings"]["description"]
    schema = properties["profile"]
    assert schema["type"] == "string" and "grounder_list_providers" in schema["description"] and "search_modes" in schema["description"] and "preset" in schema["description"]
    assert "runs as the user's own setting" in schema["description"]
