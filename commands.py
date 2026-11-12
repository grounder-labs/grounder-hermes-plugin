# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search`` and ``/search_presets``: choose how Grounder searches.

``/search`` chooses one thing: let Grounder pick for you based on your prompt (the default), one of its presets (Fast, Cheap, Live, Deep, Code,
Balanced), or one provider to search with alone, optionally with one of that provider's modes; if it finds nothing, Grounder picks for the user. It also chooses which live-data APIs may answer, every
one on unless the user turns it off. Every Hermes today asks in steps with ``ctx.ask_choice``: Let Grounder pick, a preset or a provider, then which
preset, or which of the provider's modes, and the live-data APIs as a card that switches one at a time. A Hermes that offers ``ctx.ask_form`` (none
ships it yet: it is a proposed addition to Hermes's plugin surface) shows the same choices as one form of two pages: the presets side by side, a provider's modes
in a drop-down, a preset lighting the providers and modes it includes where Grounder says which they are, type to filter, and Tab to the live-data APIs.
``/search_presets`` is the shortcut to the presets. Typed arguments (``/search exa``, ``/search exa:instant``, ``/search fast``, ``/search auto``,
``/search apis``) skip the questions. On Slack ``/search`` is Slack's own command, so ``/hermes search`` runs this one.

A preset and a pinned provider clear each other. They go to ``web.grounder.preset`` and ``web.grounder.server`` (with the provider's mode in
``web.grounder.server_mode``), the keys Hermes's own Grounder provider reads. A pinned provider is the only one a search calls (``exclusive``, Grounder 0.8.16, with
``fallback`` on: when it finds nothing Grounder picks); against a Grounder from before that it is sent once as always included (``provider._send_chosen``). The APIs turned OFF go to ``web.grounder.live_data_off``,
so one Grounder adds later is on by default. A signed-in search sends them (``provider.chosen``), and what the user chose wins over what the model passes
for the same provider or API; a free search sends none, since Grounder refuses a caller with no account all of them. A choice Grounder stops offering is
dropped when its 400 names it (``provider._send_chosen``). An ``api:<id>`` server saved by an earlier version keeps working until the next choice made
here, which replaces it, and ``/search api:<id>`` still chooses one. The lists are Grounder's public ones, fetched when a command runs and never kept, so
a new provider, mode, preset or API is on offer as soon as Grounder lists it. With no search key and no sign-in a command answers with how to sign in
instead of a list.
"""

from __future__ import annotations

from typing import Any, Optional

from . import provider as api

AUTO = "auto"
AUTO_LABEL = "Let Grounder pick for you based on your prompt"
APIS = "apis"  # the typed argument, and the step that leads to the live-data APIs where there is no second page
PICK_TAB = "Search"
APIS_TAB = "Live-data APIs"
FILTER_PROMPT = "Type to filter"
NO_MATCH = "Nothing matches"
PICK_HINT = "↑/↓ move · ←/→ presets and modes · type to filter · Tab live-data APIs · Enter confirms"
APIS_HINT = "↑/↓ move · Space checks or unchecks · type to filter · Tab search · Enter confirms"
PICK_ALONE_HINT = "↑/↓ move · ←/→ presets and modes · type to filter · Enter confirms"  # a form of one page has no Tab
APIS_ALONE_HINT = "↑/↓ move · Space checks or unchecks · type to filter · Enter confirms"
PRESETS_HEADER, PROVIDERS_HEADER = "Presets", "Providers"
PICK_QUESTION = "How should Grounder search?"
PIN_NOTE = "Only this provider; if it finds nothing, Grounder picks for you."
# The most choices one ask_choice card shows where a messaging platform caps them and drops the rest without a word: Discord's
# buttons stop at 24 (one slot is "Other"); WhatsApp Cloud's list takes 10. Where it does, a longer list is asked in pages.
# Telegram, Slack and the local surfaces show every choice.
SURFACE_CHOICE_CAPS = {"discord": 24, "whatsapp_cloud": 10}
_MORE, _BACK = "__more__", "__back__"
MAX_TOGGLES = 80  # a switch-one-at-a-time card is asked again after each switch, so it ends here if nobody ends it
# A provider's default search under another name (workers/router/src/lib/routing-settings.ts DEFAULT_ALIASES): Grounder lists it as an offering but refuses
# it as a mode of its own (a request that names it is a 400), so it is the provider itself and has no row of its own.
DEFAULT_ALIASES = {"parallel": "fast", "valyu": "all"}


def sign_in_first(what: str) -> str:
    """What a command answers when nobody is signed in: how to, and what a search does until then."""
    return (f"Sign in to choose {what}: run `{api.SIGN_IN}`, or set a search key in `{api.KEY_ENV}` ({api.KEYS_URL}). "
            f"Until then Grounder searches with its free searches, and picks the providers and modes itself.")


def _list(url: str, what: str) -> tuple[Optional[dict[str, Any]], str]:
    """``(body, "")``, or ``(None, the words to answer with)``: the sign-in hint, or why the list could not be read."""
    try:
        body, error = api.fetch_list(url)
    except api.ProviderNotSignedIn:
        return None, sign_in_first(what)
    return (body, "") if body is not None else (None, f"Could not load Grounder's list of {what}: {error}")


def _rows(rows: Any, key: str) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict) and row.get(key)] if isinstance(rows, list) else []


def _entry(row: dict[str, Any], key: str) -> dict[str, Any]:
    """``{"id", "name", "description"}`` from a row; a missing name reads as the id."""
    return {"id": str(row[key]), "name": str(row.get("display_name") or row.get("name") or row[key]), "description": str(row.get("description") or "")}


def _entries(rows: Any, key: str) -> list[dict[str, Any]]:
    """The rows of a list that have an id, as entries; a row with no id is dropped."""
    return [_entry(row, key) for row in _rows(rows, key)]


def _presets(rows: Any) -> list[dict[str, Any]]:
    """The presets, each with ``includes``: the offering types it uses, or None when Grounder does not say (an older API), so nothing lights."""
    out = []
    for row in _rows(rows, "id"):
        types = row.get("includes")
        out.append({**_entry(row, "id"), "includes": [t for t in types if isinstance(t, str)] if isinstance(types, list) and types else None})
    return out


def _providers(rows: Any) -> list[dict[str, Any]]:
    """The providers, each with ``modes``: ``[{"id": "exa:instant", "mode": "instant"}]``, the offerings that are not its default search."""
    out = []
    for row in _rows(rows, "provider"):
        entry = _entry(row, "provider")
        offerings = row.get("offerings") if isinstance(row.get("offerings"), list) else []
        modes = [o["mode"] for o in offerings if isinstance(o, dict) and isinstance(o.get("mode"), str) and api._ID.match(o["mode"])
                 and o["mode"] != DEFAULT_ALIASES.get(entry["id"])]
        out.append({**entry, "modes": [{"id": f"{entry['id']}:{mode}", "mode": mode} for mode in dict.fromkeys(modes)]})
    return out


class Catalog:
    """Grounder's lists as they are now: the presets, the providers with their modes, and the live-data APIs."""

    def __init__(self, presets: list[dict[str, Any]], providers: list[dict[str, Any]], apis: list[dict[str, str]]):
        self.presets, self.providers, self.apis = presets, providers, apis

    def provider(self, ident: str) -> Optional[dict[str, Any]]:
        return next((p for p in self.providers if p["id"] == ident), None)

    def mode_of(self, offering: str) -> Optional[tuple[dict[str, Any], dict[str, str]]]:
        """``(provider, mode)`` for an offering type such as ``exa:instant``, or None."""
        owner = self.provider(offering.partition(":")[0])
        mode = next((m for m in owner["modes"] if m["id"] == offering), None) if owner else None
        return (owner, mode) if mode else None

    def name(self, value: str) -> str:
        """The name of a pick (``preset:<id>``, ``provider:<id>``, ``mode:<offering>``, ``api:<id>``), or its id when Grounder no longer lists it."""
        kind, _, ident = value.partition(":")
        if kind == "mode":
            found = self.mode_of(ident)
            return f"{found[0]['name']} {found[1]['mode']}" if found else ident
        rows = {"preset": self.presets, "provider": self.providers, "api": self.apis}.get(kind, [])
        return next((row["name"] for row in rows if row["id"] == ident), ident)

    def includes(self, preset: dict[str, Any]) -> list[str]:
        """The pick values (``provider:<id>``, ``mode:<offering>``) a preset's ``includes`` names that Grounder lists; an offering it does not list is left out."""
        out = []
        for offering in preset.get("includes") or []:
            if self.provider(offering):
                out.append(f"provider:{offering}")
            elif self.mode_of(offering):
                out.append(f"mode:{offering}")
        return out


def _catalog(need: str = "both") -> tuple[Optional[Catalog], str]:
    """The lists a command needs, or the words to answer with. ``need`` is ``both`` (providers and APIs, then presets) or ``presets``."""
    presets: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    apis: list[dict[str, str]] = []
    if need == "both":
        body, problem = _list(api.PROVIDERS_URL, "providers")
        if body is None:
            return None, problem
        providers, apis = _providers(body.get("providers")), _entries(body.get("live_data_apis"), "id")
    body, problem = _list(api.PROFILES_URL, "presets")
    if body is None:
        return None, problem.replace("choose presets", "choose a preset")
    presets = _presets(body.get("profiles"))
    return Catalog(presets, providers, apis), ""


_save = api.save_settings


def _cap(ctx) -> Optional[int]:
    """The most choices one card shows on this surface, when it caps them; None where it does not, or where Hermes names no surface."""
    surface = getattr(ctx, "session_surface", None)
    try:
        return SURFACE_CHOICE_CAPS.get(surface()) if callable(surface) else None
    except Exception:  # a Hermes that cannot name the surface shows the whole card, as it did before surfaces were named
        return None


def _ask_paged(ctx, question: str, choices: list[dict[str, str]], keep: tuple = (), page: int = 0) -> tuple[Optional[str], int]:
    """``ctx.ask_choice`` as ``(picked, page it was picked on)``, in pages of the surface's cap when it has one and the list is longer: every page
    has "More…" and "← Previous" where there is a page on that side, and the ``keep`` choices (such as Done), so no page is a dead end.
    None for ``picked`` when nothing can ask here or the user did not pick."""
    cap = _cap(ctx)
    keep = list(keep)
    if cap is None or len(keep) + len(choices) <= cap:
        return ctx.ask_choice(question, keep + choices), 0
    size = max(1, cap - len(keep) - 2)
    pages = -(-len(choices) // size)
    page = max(0, min(page, pages - 1))
    while True:
        around = ([{"value": _BACK, "label": "← Previous"}] if page else []) + ([{"value": _MORE, "label": "More…"}] if page < pages - 1 else [])
        picked = ctx.ask_choice(question, keep + choices[page * size:(page + 1) * size] + around)
        if picked == _MORE:
            page += 1
        elif picked == _BACK:
            page -= 1
        else:
            return picked, page


def _ask(ctx, question: str, choices: list[dict[str, str]]) -> Optional[str]:
    return _ask_paged(ctx, question, choices)[0]


def _has_form(ctx) -> bool:
    """Whether this Hermes can show ``ask_form`` here. A Hermes from before it has neither method, and where a surface cannot show one ``supports_form`` says so."""
    ask, supports = getattr(ctx, "ask_form", None), getattr(ctx, "supports_form", None)
    try:
        return callable(ask) and callable(supports) and bool(supports())
    except Exception:
        return False


def _current_pick(saved: dict[str, Any], catalog: Catalog) -> str:
    """The pick the form starts on: what is saved now, where the page has a row for it. A preset or provider Grounder no longer lists, and a server saved by an
    earlier version as a live-data API, have no row, so the form starts on "Let Grounder pick" and (see ``_form``) leaves them saved unless the user picks another."""
    if saved["preset"]:
        return f"preset:{saved['preset']}" if any(p["id"] == saved["preset"] for p in catalog.presets) else AUTO
    if saved["server"].startswith("provider:") and catalog.provider(saved["server"].partition(":")[2]):
        mode = f"{saved['server'].partition(':')[2]}:{saved['server_mode']}" if saved["server_mode"] else ""
        return f"mode:{mode}" if mode and catalog.mode_of(mode) else saved["server"]
    return AUTO


def _pick_choices(catalog: Catalog) -> list[dict[str, Any]]:
    """Page one's rows: Grounder's own choice on top, a row of presets (each lighting what it includes), then the providers, each with its modes."""
    choices: list[dict[str, Any]] = [{"value": AUTO, "label": AUTO_LABEL, "description": "Grounder chooses the providers, modes and any preset for each search."},
                                     {"header": PRESETS_HEADER, "row": True}]
    for preset in catalog.presets:
        lights = catalog.includes(preset)
        choices.append({"value": f"preset:{preset['id']}", "label": preset["name"], "description": preset["description"], **({"lights": lights} if lights else {})})
    choices.append({"header": PROVIDERS_HEADER})
    for owner in catalog.providers:
        choices.append({"value": f"provider:{owner['id']}", "label": owner["name"], "description": f"{owner['description']} {PIN_NOTE}".strip()})
        choices += [{"value": f"mode:{m['id']}", "label": m["mode"], "parent": f"provider:{owner['id']}", "description": f"{owner['name']}'s {m['mode']} mode. {PIN_NOTE}"}
                    for m in owner["modes"]]
    return choices


def _pick_page(catalog: Catalog, saved: dict[str, Any]) -> dict[str, Any]:
    """Page one. ``optional``: it is answered from the start with what is saved now, so Enter on either page confirms the form."""
    return {"id": "pick", "tab": PICK_TAB, "question": PICK_QUESTION, "choices": _pick_choices(catalog), "optional": True,
            "selected": _current_pick(saved, catalog), "filter": FILTER_PROMPT, "no_match": NO_MATCH, "hint": PICK_HINT}


def _apis_page(catalog: Catalog, saved: dict[str, Any]) -> dict[str, Any]:
    """Page two: every live-data API checked, but the ones the user turned off. ``optional``, so Enter on page one confirms the form without visiting it."""
    return {"id": APIS, "tab": APIS_TAB, "question": "Which live-data APIs may Grounder use? Every one is on unless you turn it off.", "multi_select": True, "optional": True,
            "choices": [{"value": a["id"], "label": a["name"]} for a in catalog.apis], "checked": [a["id"] for a in catalog.apis if a["id"] not in saved["off"]],
            "filter": FILTER_PROMPT, "no_match": NO_MATCH, "hint": APIS_HINT}


def _pages(catalog: Catalog, saved: dict[str, Any]) -> list[dict[str, Any]]:
    """The form's pages: the pick, then the live-data APIs where Grounder lists any (with none that page would be empty, and the pick is the only page)."""
    pick = _pick_page(catalog, saved)
    return [pick, _apis_page(catalog, saved)] if catalog.apis else [{**pick, "hint": PICK_ALONE_HINT}]


def _pick_changes(value: str) -> dict[str, Any]:
    """What a pick saves. A preset and a pinned provider clear each other, "Let Grounder pick" clears both, and either replaces a server saved as an API by an earlier version."""
    kind, _, ident = value.partition(":")
    if kind == "preset":
        return {"preset": ident, "server": None, "server_mode": None}
    if value == AUTO:
        return {"preset": None, "server": None, "server_mode": None}
    if kind == "mode":
        owner, _, mode = ident.partition(":")
        return {"preset": None, "server": f"provider:{owner}", "server_mode": mode}
    return {"preset": None, "server": value, "server_mode": None}


def _pick_words(value: str, catalog: Catalog, saved: dict[str, Any]) -> str:
    kind = value.partition(":")[0]
    old = saved["server"] if saved["server"].startswith("api:") and saved["server"] != value else ""
    note = f" It no longer always includes {catalog.name(old)}." if old else ""
    if value == AUTO:
        return "Grounder will pick the providers, modes and any preset for each search from your prompt." + note
    if kind == "preset":
        return f"Grounder will search with the {catalog.name(value)} preset." + note
    if kind == "mode":
        found = catalog.mode_of(value.partition(":")[2])
        return (f"Grounder will search with only {found[0]['name']}'s {found[1]['mode']} mode; if it finds nothing, Grounder picks for you." if found
                else f"Grounder will search with only {catalog.name(value)}; if it finds nothing, Grounder picks for you.") + note
    if kind == "provider":
        return f"Grounder will search with only {catalog.name(value)}; if it finds nothing, Grounder picks for you." + note
    return f"Grounder will always include {catalog.name(value)} in searches." + note


def _apis_words(off: list[str], catalog: Catalog) -> str:
    if not off:
        return "Every live-data API is on."
    names = ", ".join(catalog.name(f"api:{i}") for i in off)
    return f"Live-data APIs turned off: {names}."


def _off_from(checked: list[str], catalog: Catalog) -> list[str]:
    """The live-data APIs to save as off: every listed one left unchecked, in Grounder's order."""
    return [a["id"] for a in catalog.apis if a["id"] not in set(checked)]


def _form(ctx, catalog: Catalog, saved: dict[str, Any], pages: list[dict[str, Any]]) -> str:
    """Show the pages as one form and save what came back."""
    answers = ctx.ask_form(pages)
    if answers is None:
        return "Nothing changed."
    changes: dict[str, Any] = {}
    words = []
    pick = answers.get("pick")
    # Page one starts on what is saved, and counts as answered from the start: a pick equal to that is the page left alone, which must not clear
    # a pin it cannot show (one Grounder no longer lists, or an API saved by an earlier version), so only a different pick saves.
    if pick and pick != _current_pick(saved, catalog):
        changes.update(_pick_changes(pick))
        words.append(_pick_words(pick, catalog, saved))
    if isinstance(answers.get(APIS), list):
        off = _off_from(answers[APIS], catalog)
        if set(off) != set(saved["off"]):
            changes[api.LIVE_OFF_KEY] = off
            words.append(_apis_words(off, catalog))
    if not changes:
        return "Nothing changed."
    _save(**changes)
    return " ".join(words)


def _preset_text(preset: dict[str, Any], catalog: Catalog) -> str:
    """A preset's description, and where Grounder says which providers and modes it uses, how many of each (a list of names would be most of the catalog)."""
    values = catalog.includes(preset)
    owners = len(dict.fromkeys(value.partition(":")[2].partition(":")[0] for value in values))
    modes = sum(value.startswith("mode:") for value in values)
    uses = f" Uses {owners} provider{'s' if owners != 1 else ''}" + (f" and {modes} mode{'s' if modes != 1 else ''}" if modes else "") + "."
    return preset["description"] + (uses if values else "")


def _step_pick(ctx, catalog: Catalog) -> Optional[str]:
    """Page one where there is no form, in steps: Let Grounder pick, a preset or a provider; then which preset, or which of the provider's modes."""
    top = [{"value": AUTO, "label": AUTO_LABEL},
           {"value": "presets", "label": "A preset…", "description": ", ".join(p["name"] for p in catalog.presets)},
           {"value": "providers", "label": "A provider…", "description": "search with only one, and optionally one of its modes; if it finds nothing, Grounder picks for you"},
           {"value": APIS, "label": "Live-data APIs…", "description": "choose which ones Grounder may use"}]
    group = ctx.ask_choice(PICK_QUESTION, top)
    if group in (None, AUTO, APIS):
        return group
    if group == "presets":
        return _ask(ctx, "Which preset should Grounder search with?",
                    [{"value": f"preset:{p['id']}", "label": p["name"], "description": _preset_text(p, catalog)} for p in catalog.presets])
    picked = _ask(ctx, "Which provider should Grounder search with, and only it?",
                  [{"value": f"provider:{p['id']}", "label": p["name"], "description": f"{len(p['modes'])} mode{'s' if len(p['modes']) != 1 else ''}" if p["modes"] else ""} for p in catalog.providers])
    owner = catalog.provider(picked.partition(":")[2]) if picked else None
    if not owner or not owner["modes"]:
        return picked
    return _ask(ctx, f"Which {owner['name']} mode?",
                [{"value": picked, "label": "Any mode", "description": f"{owner['name']}'s default search"},
                 *({"value": f"mode:{m['id']}", "label": m["mode"]} for m in owner["modes"])])


def _step_apis(ctx, catalog: Catalog) -> str:
    """The live-data APIs where there is no second page: a card that switches one API each time it is asked, until Done. Each switch is saved at once.
    Done is on every page of a card a surface caps, and the card comes back to the page the switch was on."""
    saved = api.settings()
    off = list(saved["off"])
    page = 0
    for _ in range(MAX_TOGGLES):
        choices = [{"value": a["id"], "label": f"{'✗ off' if a['id'] in off else '✓ on'}: {a['name']}"} for a in catalog.apis]
        picked, page = _ask_paged(ctx, "Live-data APIs Grounder may use. Pick one to switch it on or off, or Done.", choices,
                                  keep=({"value": "done", "label": "Done"},), page=page)
        if picked in (None, "done"):
            break
        off = [i for i in off if i != picked] if picked in off else [*off, picked]
        _save(**{api.LIVE_OFF_KEY: _off_from([a["id"] for a in catalog.apis if a["id"] not in off], catalog)})
    return _apis_words(api.settings()["off"], catalog)


def _listing(catalog: Catalog, usage: str) -> str:
    lines = ["Grounder's presets:", *(f"  {p['id']} ({p['name']}): {p['description']}" for p in catalog.presets),
             "Grounder's providers, each with its modes:",
             *(f"  {p['id']} ({p['name']})" + (f": {', '.join(m['mode'] for m in p['modes'])}" if p["modes"] else "") for p in catalog.providers)]
    if catalog.apis:
        lines += ["Live-data APIs, all on unless turned off:", *(f"  {a['id']} ({a['name']})" for a in catalog.apis)]
    return "\n".join([*lines, usage])


def _match(arg: str, catalog: Catalog) -> tuple[Optional[str], str]:
    """``(pick, problem)`` for a typed argument: an id or a name of a preset, a provider or an API, or a provider's mode as ``<provider>:<mode>``;
    ``preset:``, ``provider:``, ``mode:`` (an older name for ``preset:``, or ``mode:<provider>:<mode>``) or ``api:`` first when the same word is more than one of them."""
    kind, _, ident = arg.partition(":") if arg.partition(":")[0] in ("preset", "provider", "mode", "api") else ("", "", arg)
    found = [f"{prefix}:{row['id']}" for prefix, rows in (("preset", catalog.presets), ("provider", catalog.providers), ("api", catalog.apis))
             if kind in ("", prefix) or (kind == "mode" and prefix == "preset") for row in rows if ident in (row["id"].lower(), row["name"].lower())]
    if kind in ("", "mode"):
        found += [f"mode:{m['id']}" for p in catalog.providers for m in p["modes"] if ident == m["id"].lower()]
    if len(found) == 1:
        return found[0], ""
    if found:
        return None, f"{ident} is more than one thing: use " + " or ".join(f"`/search {f}`" for f in found) + "."
    return None, f"Grounder has no preset, provider or mode called {arg}."


def search_command(ctx, raw_args: str) -> str:
    """``/search [<id>|auto|apis]``: one form for the preset or provider and the live-data APIs, or an argument that skips it."""
    catalog, problem = _catalog()
    if catalog is None:
        return problem
    saved = api.settings()
    usage = "Use `/search <id>` to use a preset or search with only one provider (`/search <provider>:<mode>` for one of its modes), `/search auto` to let Grounder pick, or `/search apis` to choose the live-data APIs."
    arg = (raw_args or "").strip().lower()
    if not arg:
        if _has_form(ctx):
            return _form(ctx, catalog, saved, _pages(catalog, saved))
        arg = (_step_pick(ctx, catalog) or "").lower()
        if not arg:
            return _listing(catalog, usage)
    if arg == APIS:
        if _has_form(ctx):
            return _form(ctx, catalog, saved, [{**_apis_page(catalog, saved), "optional": False, "hint": APIS_ALONE_HINT}])
        return _step_apis(ctx, catalog)
    pick = arg if arg == AUTO else _match(arg, catalog)[0]
    if pick is None:
        return f"{_match(arg, catalog)[1]}\n{_listing(catalog, usage)}"
    _save(**_pick_changes(pick))
    return _pick_words(pick, catalog, saved)


def presets_command(ctx, raw_args: str) -> str:
    """``/search_presets [<id>|auto]``: the shortcut to the presets. A preset clears a pinned provider; ``auto`` clears the preset and leaves a provider."""
    catalog, problem = _catalog("presets")
    if catalog is None:
        return problem
    saved = api.settings()
    usage = "Use `/search_presets <id>` to search with one, or `/search_presets auto` to let Grounder pick."
    listing = "Grounder's presets:\n" + "\n".join(f"  {p['id']} ({p['name']}): {p['description']}" for p in catalog.presets)
    arg = (raw_args or "").strip().lower()
    if not arg:
        choices = [{"value": AUTO, "label": AUTO_LABEL}, *({"value": p["id"], "label": p["name"], "description": p["description"]} for p in catalog.presets)]
        arg = (ctx.ask_choice("Which preset should Grounder search with?", choices) or "").lower()
        if not arg:
            return f"{listing}\n{usage}"
    if arg == AUTO:
        _save(preset=None)
        return "Grounder will pick the preset for each search from your prompt."
    chosen = next((p for p in catalog.presets if arg in (p["id"].lower(), p["name"].lower())), None)
    if chosen is None:
        return f"Grounder has no preset called {arg}.\n{listing}\n{usage}"
    _save(**_pick_changes(f"preset:{chosen['id']}"))
    return _pick_words(f"preset:{chosen['id']}", catalog, saved)


def register_commands(ctx) -> None:
    ctx.register_command(
        "search", lambda raw_args: search_command(ctx, raw_args), args_hint="[provider|provider:mode|preset|auto|apis]",
        description="Choose how Grounder searches: a preset, or a provider and its mode, and which live-data APIs it may use (on Slack: /hermes search)")
    ctx.register_command(
        "search_presets", lambda raw_args: presets_command(ctx, raw_args), args_hint="[preset|auto]",
        description="Choose the preset Grounder searches with, or let Grounder pick")
