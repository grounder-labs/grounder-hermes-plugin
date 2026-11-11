# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search`` and ``/search_presets``: choose the server or the mode Grounder searches with.

Both save to ``web.grounder.server`` and ``web.grounder.preset`` in the profile's ``config.yaml``, the keys Hermes's own
Grounder provider reads, so a user who moves to it keeps their choice. A signed-in search sends them (``provider.chosen``); a
free search, with no credential, sends neither, because Grounder refuses a caller with no account both. The lists are Grounder's
public ones, fetched when a command runs and never kept, so a new server or mode is on offer as soon as Grounder lists it.
With no search key and no sign-in a command answers with how to sign in instead of a list.
"""

from __future__ import annotations

from typing import Any, Optional

from . import provider as api

AUTO = "auto"
AUTO_LABEL = "Let Grounder pick"
# The most choices one ask_choice card shows where a messaging platform caps them and drops the rest without a word: Discord's
# buttons stop at 24 (one slot is "Other"); WhatsApp Cloud's list takes 10. Where it does, a longer list is asked in steps.
# Telegram, Slack and the local surfaces show every choice.
SURFACE_CHOICE_CAPS = {"discord": 24, "whatsapp_cloud": 10}
_MORE = "__more__"


def sign_in_first(what: str) -> str:
    """What a command answers when nobody is signed in: how to, and what a search does until then."""
    return (f"Sign in to choose {what}: run `{api.SIGN_IN}`, or set a search key in `{api.KEY_ENV}` ({api.KEYS_URL}). "
            f"Until then Grounder searches with its free searches, and picks the servers and modes itself.")


def _list(url: str, what: str, listed: str) -> tuple[Optional[dict[str, Any]], str]:
    """``(body, "")``, or ``(None, the words to answer with)``: the sign-in hint, or why the list could not be read."""
    try:
        body, error = api.fetch_list(url)
    except api.ProviderNotSignedIn:
        return None, sign_in_first(what)
    return (body, "") if body is not None else (None, f"Could not load Grounder's list of {listed}: {error}")


def _entries(rows: Any, key: str) -> list[dict[str, str]]:
    """``[{"id", "name"}]`` from a list; a row with no id is dropped, and a missing name reads as the id."""
    return [{"id": str(row[key]), "name": str(row.get("display_name") or row.get("name") or row[key])}
            for row in rows if isinstance(row, dict) and row.get(key)] if isinstance(rows, list) else []


def _save(key: str, value: Optional[str]) -> None:
    """Set ``web.grounder.<key>``, or remove it with ``None``, in the active profile's config."""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    web = config.get("web") if isinstance(config.get("web"), dict) else config.setdefault("web", {})
    section = web.get(api.SETTINGS_KEY) if isinstance(web.get(api.SETTINGS_KEY), dict) else web.setdefault(api.SETTINGS_KEY, {})
    if value is None:
        section.pop(key, None)
    else:
        section[key] = value
    save_config(config)


def _cap(ctx) -> Optional[int]:
    """The most choices one card shows on this surface, when it caps them; None where it does not, or where Hermes names no surface."""
    surface = getattr(ctx, "session_surface", None)
    try:
        return SURFACE_CHOICE_CAPS.get(surface()) if callable(surface) else None
    except Exception:  # a Hermes that cannot name the surface shows the whole card, as it did before surfaces were named
        return None


def _ask(ctx, question: str, choices: list[dict[str, str]]) -> Optional[str]:
    """``ctx.ask_choice``, in pages of the surface's cap when it has one and the list is longer (the last choice of a full page is "More…").
    None when nothing can ask here or the user did not pick."""
    cap = _cap(ctx)
    if cap is None or len(choices) <= cap:
        return ctx.ask_choice(question, choices)
    size = cap - 1
    for start in range(0, len(choices), size):
        last = start + size >= len(choices)
        page = choices[start:start + size] + ([] if last else [{"value": _MORE, "label": "More…"}])
        picked = ctx.ask_choice(question, page)
        if picked != _MORE:
            return picked
    return None


def _ask_server(ctx, providers: list[dict[str, str]], apis: list[dict[str, str]]) -> Optional[str]:
    """The ``provider:<id>``, ``api:<id>`` or ``auto`` the user picks. Where the surface caps a card and the whole list is longer, two steps:
    "Let Grounder pick", "Search providers" and "Live-data APIs", then that list."""
    question = "Which server should Grounder always use for searches?"
    auto = {"value": AUTO, "label": AUTO_LABEL}
    pick_provider = [{"value": f"provider:{p['id']}", "label": p["name"], "description": "search provider"} for p in providers]
    pick_api = [{"value": f"api:{a['id']}", "label": a["name"], "description": "live-data API"} for a in apis]
    cap = _cap(ctx)
    if cap is None or 1 + len(pick_provider) + len(pick_api) <= cap:
        return ctx.ask_choice(question, [auto, *pick_provider, *pick_api])
    group = ctx.ask_choice(question, [auto, {"value": "providers", "label": "Search providers"}, {"value": "apis", "label": "Live-data APIs"}])
    if group in (None, AUTO):
        return group
    return _ask(ctx, question, pick_provider if group == "providers" else pick_api)


def _server_text(providers: list[dict[str, str]], apis: list[dict[str, str]]) -> str:
    lines = ["Grounder's search providers:", *(f"  {p['id']} ({p['name']})" for p in providers)]
    if apis:
        lines += ["Live-data APIs:", *(f"  {a['id']} ({a['name']})" for a in apis)]
    return "\n".join(lines)


def _match_server(arg: str, providers: list[dict[str, str]], apis: list[dict[str, str]]) -> tuple[Optional[str], Optional[str], str]:
    """``(value, name, problem)`` for ``/search <arg>``: ``provider:<id>`` or ``api:<id>``, the prefix being optional."""
    kind, _, ident = arg.partition(":") if ":" in arg else ("", "", arg)
    found = [(f"{prefix}:{row['id']}", row["name"]) for prefix, rows in (("provider", providers), ("api", apis))
             if kind in ("", prefix) for row in rows if row["id"].lower() == ident]
    if len(found) == 1:
        return found[0][0], found[0][1], ""
    if found:
        return None, None, f"{ident} is both a search provider and a live-data API; use `/search provider:{ident}` or `/search api:{ident}`."
    return None, None, f"Grounder has no server called {arg}."


def search_command(ctx, raw_args: str) -> str:
    """``/search [<id>|auto]``: always use one provider or live-data API, or let Grounder pick."""
    body, problem = _list(api.PROVIDERS_URL, "servers", "servers")
    if body is None:
        return problem
    providers, apis = _entries(body.get("providers"), "provider"), _entries(body.get("live_data_apis"), "id")
    usage = "Use `/search <id>` to always use one, or `/search auto` to let Grounder pick."
    arg = (raw_args or "").strip().lower()
    if not arg:
        arg = (_ask_server(ctx, providers, apis) or "").lower()
        if not arg:
            return f"{_server_text(providers, apis)}\n{usage}"
    if arg == AUTO:
        _save("server", None)
        return "Grounder picks the providers for each search."
    value, name, problem = _match_server(arg, providers, apis)
    if value is None:
        return f"{problem}\n{_server_text(providers, apis)}\n{usage}"
    _save("server", value)
    return f"Grounder will always use {name} for searches."


def presets_command(ctx, raw_args: str) -> str:
    """``/search_presets [<id>|auto]``: always search in one of Grounder's modes, or let Grounder pick."""
    body, problem = _list(api.PROFILES_URL, "modes", "modes")
    if body is None:
        return problem
    profiles = [{"id": str(p["id"]), "name": str(p.get("name") or p["id"]), "description": str(p.get("description") or "")}
                for p in body.get("profiles") or [] if isinstance(p, dict) and p.get("id")]
    usage = "Use `/search_presets <id>` to always use one, or `/search_presets auto` to let Grounder pick."
    listing = "Grounder's modes:\n" + "\n".join(f"  {p['id']} ({p['name']}): {p['description']}" for p in profiles)
    arg = (raw_args or "").strip().lower()
    if not arg:
        choices = [{"value": AUTO, "label": AUTO_LABEL}, *({"value": p["id"], "label": p["name"], "description": p["description"]} for p in profiles)]
        arg = (ctx.ask_choice("Which mode should Grounder search in?", choices) or "").lower()
        if not arg:
            return f"{listing}\n{usage}"
    if arg == AUTO:
        _save("preset", None)
        return "Grounder picks the mode for each search."
    chosen = next((p for p in profiles if p["id"].lower() == arg or p["name"].lower() == arg), None)
    if chosen is None:
        return f"Grounder has no mode called {arg}.\n{listing}\n{usage}"
    _save("preset", chosen["id"])
    return f"Grounder will search in {chosen['name']} mode."


def register_commands(ctx) -> None:
    ctx.register_command(
        "search", lambda raw_args: search_command(ctx, raw_args), args_hint="[server-id|auto]",
        description="Choose the server Grounder always searches with, or let Grounder pick")
    ctx.register_command(
        "search_presets", lambda raw_args: presets_command(ctx, raw_args), args_hint="[mode-id|auto]",
        description="Choose the mode Grounder searches in, or let Grounder pick")
