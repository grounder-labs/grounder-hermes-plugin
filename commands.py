# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search``, the plugin's one command: choose how Grounder searches.

Grounder has three modes, and ``/search`` chooses one: **Grounder optimized** (Grounder picks the provider for the user's question; the default), a **Grounder preset**
(Fast, Cheap, Live, Deep, Code or Balanced: Grounder picks the best provider for that preset) or **Grounder direct** (the user picks the provider, and optionally one of
its modes: that provider is the only search provider called, and what it finds is what the user gets, even when that is nothing). Every mode returns the provider's own results, each uncut and in its own
order. ``/search`` also sets each live-data API, in every mode, to **Every call**, **Never** or **Grounder decides** (the default: asked only when the question's features call for it).
Every Hermes today asks with ``ctx.ask_choice``, and ``/search`` opens a hub, "How should Grounder search?": Let Grounder pick (Grounder optimized), A preset, A provider, Live-data APIs
and Done, the current choice marked "(current)". Each choice opens its own screen (the presets, each with its purpose and the providers it uses; the providers, then one provider's modes;
the live-data APIs, then one API's Every call, Grounder decides or Never) and every screen has a "← Back" row that returns, so the hub is where the user always is. Done answers with one line
saying what is set now (the command's reply, which every surface shows: ``ctx.post_message`` answers ok in the classic CLI's gateway and shows nothing), only when something changed. A Hermes that offers ``ctx.ask_form`` (none ships it yet: it is a proposed addition to Hermes's plugin surface)
shows the same choices as one form of two pages instead. Typed arguments (``/search exa``, ``/search exa:instant``, ``/search fast``, ``/search auto`` or ``/search optimized``,
``/search apis`` or ``/search apis espn=every mlb=never``) skip the hub. On Slack ``/search`` is Slack's own command, so ``/hermes search`` runs this one.

A preset and a pinned provider clear each other. They go to ``web.grounder.preset`` and ``web.grounder.server`` (with the provider's mode in
``web.grounder.server_mode``), the keys Hermes's own Grounder provider reads. A pinned provider is the only search provider a search calls (``exclusive``, Grounder 0.8.16, with
``fallback`` off: when it finds nothing the search returns nothing); against a Grounder from before that it is sent once as always included (``provider._send_chosen``). The live-data APIs
set to Never go to ``web.grounder.live_data_off`` and those set to Every call to ``web.grounder.live_data_always``, so one Grounder adds later is Grounder decides. A signed-in search sends
them in every mode (``provider.chosen``) and names its mode (``provider._send``), and what the user chose wins over what the model passes
for the same provider or API; a free search sends none, since Grounder refuses a caller with no account all of them. A choice Grounder stops offering is
dropped when its 400 names it (``provider._send_chosen``). An ``api:<id>`` server saved by an earlier version keeps working, as Every call, until the next choice made
here, which replaces it, and ``/search api:<id>`` sets one API to Every call. The lists are Grounder's public ones, fetched when a command runs and never kept, so
a new provider, mode, preset or API is on offer as soon as Grounder lists it. With no search key and no sign-in a command answers with how to sign in
instead of a list.
"""

from __future__ import annotations

from typing import Any, Optional

from . import provider as api

AUTO = "auto"
OPTIMIZED = "optimized"  # typed as an argument, the same as ``auto``
OPTIMIZED_HEADER = "Grounder optimized"
AUTO_LABEL = "Grounder picks the provider for your question"
AUTO_NOTE = "The default. The provider's own results, each uncut and in its own order."
APIS = "apis"  # the typed argument, and the step that leads to the live-data APIs where there is no second page
PICK_TAB = "Search"
APIS_TAB = "Live-data APIs"
FILTER_PROMPT = "Type to filter"
NO_MATCH = "Nothing matches"
PICK_HINT = "↑/↓ move · ←/→ presets and modes · type to filter · Tab live-data APIs · Enter confirms"
APIS_HINT = "↑/↓ move · ←/→ + Space sets · type to filter · Tab search · Enter confirms"
PICK_ALONE_HINT = "↑/↓ move · ←/→ presets and modes · type to filter · Enter confirms"  # a form of one page has no Tab
APIS_ALONE_HINT = "↑/↓ move · ←/→ + Space sets · type to filter · Enter confirms"
# A live-data API's three settings, in the order the page shows them, the words the user types for each, and the note under each while the cursor is on it. ``auto`` is the default
# and is sent as nothing (provider.chosen); Every call is ``always`` and Never is ``off`` in Grounder's routing settings.
LIVE_STATES = (("always", "Every call"), ("off", "Never"), ("auto", "Grounder decides"))
LIVE_LABEL = dict(LIVE_STATES)
LIVE_NOTES = {"always": "Grounder asks {name} on every search, in every mode, beside the search provider.",
              "off": "Grounder never asks {name}, in any mode.",
              "auto": "Grounder asks {name} only when your question's features call for it, in every mode. The default."}
LIVE_ANSWERS = {"every": "always", "always": "always", "never": "off", "off": "off", "auto": "auto", "decides": "auto"}
PRESETS_HEADER, PROVIDERS_HEADER = "Grounder preset", "Grounder direct"
PICK_QUESTION = "How should Grounder search?"
PRESET_NOTE = "Grounder picks the best provider for this preset."
PIN_NOTE = "The only search provider called."
# The most choices one ask_choice card can hold where a messaging platform caps them and drops the rest without a word, counting what Hermes adds to the card itself: Discord's
# buttons stop at 25 and Hermes keeps one for "Other"; WhatsApp Cloud's list takes 10 rows and Hermes appends an "Other" row after slicing the choices to 10, so the card holds 9 (an
# eleventh row is one WhatsApp refuses). A list is asked PAGE_SIZE items a page everywhere, fewer where a surface caps the card.
SURFACE_CHOICE_CAPS = {"discord": 24, "whatsapp_cloud": 9}
PAGE_SIZE = 8  # items a page of a list shows: with More and Back, a page fits a 24-row terminal's menu (Hermes's panel keeps the question and "Other" on screen up to 13 rows there)
_MORE = "__more__"  # the paging row: "More: 9–16 of 22"
_RETURN, _ALL, DONE = "back", "__all__", "done"  # "← Back" (a screen's way up; its value is a word, so typing "back" picks it), "Set all to Grounder decides" and the hub's Done
BACK_LABEL, CURRENT = "← Back", " (current)"
HUB_NOTE = "Grounder optimized: Grounder picks the provider for each question"
MAX_TOGGLES = 80  # a switch-one-at-a-time card is asked again after each switch, so it ends here if nobody ends it
# A provider's default search under another name (workers/router/src/lib/routing-settings.ts DEFAULT_ALIASES): Grounder lists it as an offering but refuses
# it as a mode of its own (a request that names it is a 400), so it is the provider itself and has no row of its own.
DEFAULT_ALIASES = {"parallel": "fast", "valyu": "all"}


def sign_in_first(what: str) -> str:
    """What a command answers when nobody is signed in: how to, and what a search does until then."""
    return (f"Sign in to choose {what}: run `{api.SIGN_IN}`, or set a search key in `{api.KEY_ENV}` ({api.KEYS_URL}). "
            f"Until then Grounder searches with Grounder optimized, on its free searches.")


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


def _purpose(text: str) -> str:
    """The first clause of a description, for a row that is one line: up to the first full stop, colon or semicolon, with no full stop at the end."""
    text = text.strip()
    for stop in (". ", ": ", "; "):
        text = text.partition(stop)[0]
    return text.rstrip(".").strip()


def _mode_words(offering: dict[str, Any]) -> str:
    """What a mode is, in a few words, from what Grounder lists for it: the first clause of its own note when that is short (and not a legacy mark), else its features
    (``web-search``, ``page-text``) as words. Empty when Grounder says nothing."""
    note = _purpose(str(offering.get("limits") or ""))
    if note and len(note) <= 64 and not note.lower().startswith("legacy"):
        return note
    features = [f.replace("-", " ") for f in offering.get("features") or [] if isinstance(f, str)] if isinstance(offering.get("features"), list) else []
    return ", ".join(features).capitalize()


def _providers(rows: Any) -> list[dict[str, Any]]:
    """The providers, each with ``modes``: ``[{"id": "exa:instant", "mode": "instant", "description": "Cache only, the lowest latency"}]``, the offerings that are not its default
    search, and ``default_description``, the default search's own line ("" from an API that has none). A mode's description is the one Grounder lists for it; from an API that lists none
    it is made from the vendor's note on the mode, else its features."""
    out = []
    for row in _rows(rows, "provider"):
        entry = _entry(row, "provider")
        offerings = row.get("offerings") if isinstance(row.get("offerings"), list) else []
        modes: dict[str, dict[str, str]] = {}
        default = ""
        for o in offerings:
            if not isinstance(o, dict):
                continue
            mode, said = o.get("mode"), o.get("description")
            said = said.strip() if isinstance(said, str) else ""
            if mode is None:
                default = default or said
            elif isinstance(mode, str) and api._ID.match(mode) and mode != DEFAULT_ALIASES.get(entry["id"]):
                modes.setdefault(mode, {"id": f"{entry['id']}:{mode}", "mode": mode, "description": said or _mode_words(o)})
        out.append({**entry, "modes": list(modes.values()), "default_description": default})
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


def _catalog() -> tuple[Optional[Catalog], str]:
    """The lists ``/search`` needs (the providers and live-data APIs, then the presets), or the words to answer with."""
    body, problem = _list(api.PROVIDERS_URL, "providers")
    if body is None:
        return None, problem
    providers, apis = _providers(body.get("providers")), _entries(body.get("live_data_apis"), "id")
    body, problem = _list(api.PROFILES_URL, "presets")
    if body is None:
        return None, problem.replace("choose presets", "choose a preset")
    return Catalog(_presets(body.get("profiles")), providers, apis), ""


_save = api.save_settings


def _cap(ctx) -> Optional[int]:
    """The most choices one card shows on this surface, when it caps them; None where it does not, or where Hermes names no surface."""
    surface = getattr(ctx, "session_surface", None)
    try:
        return SURFACE_CHOICE_CAPS.get(surface()) if callable(surface) else None
    except Exception:  # a Hermes that cannot name the surface shows the whole card, as it did before surfaces were named
        return None


def _page_size(ctx, kept: int) -> int:
    """How many items one page of a list shows: PAGE_SIZE, fewer where the surface caps a card (the page also holds its ``kept`` rows, More and Back)."""
    cap = _cap(ctx)
    return PAGE_SIZE if cap is None else max(1, min(PAGE_SIZE, cap - kept - 2))


def _ask_paged(ctx, question: str, items: list[dict[str, str]], keep: tuple = (), page: Optional[int] = None, current: Optional[str] = None,
               up: str = BACK_LABEL) -> tuple[Optional[str], int]:
    """``ctx.ask_choice`` as ``(picked, page it was picked on)``, a page of PAGE_SIZE items at a time so the question and "← Back" stay on screen: the ``keep`` rows (such as "Set all to
    Grounder decides") on every page, then the page's items, then ``More: 9–16 of 22`` where more follow, then Back. Back on the first page is ``up`` and returns ``_RETURN``; on a later
    page it goes to the page before (``← Back: 1–8 of 22``). The list opens on the page that holds ``current`` (a value), else on ``page``, else the first. None for ``picked`` when
    nothing can ask here or the user did not pick."""
    keep = list(keep)
    size = _page_size(ctx, len(keep))
    pages = max(1, -(-len(items) // size))
    if page is None:
        page = next((i // size for i, item in enumerate(items) if current and item["value"] == current), 0)
    page = max(0, min(page, pages - 1))
    while True:
        first, last = page * size, min(len(items), (page + 1) * size)
        rows = keep + items[first:last]
        if page < pages - 1:
            rows.append({"value": _MORE, "label": f"More: {last + 1}\u2013{min(len(items), last + size)} of {len(items)}"})
        rows.append({"value": _RETURN, "label": f"{BACK_LABEL}: {first - size + 1}\u2013{first} of {len(items)}" if page else up})
        picked = ctx.ask_choice(question, rows)
        if picked == _MORE:
            page += 1
        elif picked == _RETURN and page:
            page -= 1
        else:
            return picked, page


def _has_form(ctx) -> bool:
    """Whether this Hermes can show ``ask_form`` here. A Hermes from before it has neither method, and where a surface cannot show one ``supports_form`` says so."""
    ask, supports = getattr(ctx, "ask_form", None), getattr(ctx, "supports_form", None)
    try:
        return callable(ask) and callable(supports) and bool(supports())
    except Exception:
        return False


def _current_pick(saved: dict[str, Any], catalog: Catalog) -> str:
    """The pick the form starts on: what is saved now, where the page has a row for it. A preset or provider Grounder no longer lists, and a server saved by an
    earlier version as a live-data API, have no row, so the form starts on Grounder optimized and (see ``_form``) leaves them saved unless the user picks another."""
    if saved["preset"]:
        return f"preset:{saved['preset']}" if any(p["id"] == saved["preset"] for p in catalog.presets) else AUTO
    if saved["server"].startswith("provider:") and catalog.provider(saved["server"].partition(":")[2]):
        mode = f"{saved['server'].partition(':')[2]}:{saved['server_mode']}" if saved["server_mode"] else ""
        return f"mode:{mode}" if mode and catalog.mode_of(mode) else saved["server"]
    return AUTO


def _pick_choices(catalog: Catalog) -> list[dict[str, Any]]:
    """Page one's rows, under the three modes' headers: Grounder optimized (one row), Grounder preset (a row of presets, each saying what it includes and lighting it),
    then Grounder direct (the providers, each with its modes)."""
    choices: list[dict[str, Any]] = [{"header": OPTIMIZED_HEADER}, {"value": AUTO, "label": AUTO_LABEL, "description": AUTO_NOTE},
                                     {"header": PRESETS_HEADER, "row": True}]
    for preset in catalog.presets:
        lights = catalog.includes(preset)
        choices.append({"value": f"preset:{preset['id']}", "label": preset["name"], "description": _preset_text(preset, catalog), **({"lights": lights} if lights else {})})
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


def _live_state(saved: dict[str, Any], ident: str) -> str:
    """A live-data API's setting now: ``off`` (Never), ``always`` (Every call, or the older ``api:<id>`` server) or ``auto`` (Grounder decides, the default)."""
    if ident in saved["off"]:
        return "off"
    return "always" if ident in saved["always"] or saved["server"] == f"api:{ident}" else "auto"


def _live_states(saved: dict[str, Any], catalog: Catalog) -> dict[str, str]:
    return {a["id"]: _live_state(saved, a["id"]) for a in catalog.apis}


def _apis_page(catalog: Catalog, saved: dict[str, Any]) -> dict[str, Any]:
    """Page two: each live-data API a header over its three settings side by side (Every call, Never, Grounder decides), one chosen in each, the one it has now. ``optional``, so
    Enter on page one confirms the form without visiting it. A setting's value is ``<api id>:<setting>``, and its filter text names the API, so typing an API's name finds its row."""
    choices: list[dict[str, Any]] = []
    for a in catalog.apis:
        choices.append({"header": a["name"], "row": True})
        choices += [{"value": f"{a['id']}:{state}", "label": label, "match": f"{a['name']} {label}", "description": LIVE_NOTES[state].format(name=a["name"])}
                    for state, label in LIVE_STATES]
    return {"id": APIS, "tab": APIS_TAB, "question": "How should Grounder use each live-data API?", "multi_select": True, "one_per_row": True,
            "optional": True, "choices": choices, "checked": (now := [f"{ident}:{state}" for ident, state in _live_states(saved, catalog).items()]),
            "selected": now[0] if now else "", "filter": FILTER_PROMPT, "no_match": NO_MATCH, "hint": APIS_HINT}


def _pages(catalog: Catalog, saved: dict[str, Any]) -> list[dict[str, Any]]:
    """The form's pages: the pick, then the live-data APIs where Grounder lists any (with none that page would be empty, and the pick is the only page)."""
    pick = _pick_page(catalog, saved)
    return [pick, _apis_page(catalog, saved)] if catalog.apis else [{**pick, "hint": PICK_ALONE_HINT}]


def _pick_changes(value: str) -> dict[str, Any]:
    """What a pick saves. A preset and a pinned provider clear each other, Grounder optimized clears both, and either replaces a server saved as an API by an earlier version."""
    kind, _, ident = value.partition(":")
    if kind == "preset":
        return {"preset": ident, "server": None, "server_mode": None}
    if value == AUTO:
        return {"preset": None, "server": None, "server_mode": None}
    if kind == "mode":
        owner, _, mode = ident.partition(":")
        return {"preset": None, "server": f"provider:{owner}", "server_mode": mode}
    return {"preset": None, "server": value, "server_mode": None}


def _direct_tail() -> str:
    """What a Grounder direct choice says of a provider that finds nothing, as ``provider.DIRECT_FALLBACK`` has it: nothing comes back, or Grounder picks."""
    return "; if it finds nothing, Grounder picks for you." if api.DIRECT_FALLBACK else " and return what it finds, even when that is nothing."


def _direct_words(name: str) -> str:
    """Grounder direct's confirmation: the search provider is only the one picked, and the live-data APIs follow their own settings."""
    return f"Grounder direct: Grounder will use only {name} as the search provider{_direct_tail()} Live-data APIs follow your live-data choices (`/search apis`)."


def _pick_words(value: str, catalog: Catalog, saved: dict[str, Any]) -> str:
    kind = value.partition(":")[0]
    old = saved["server"] if saved["server"].startswith("api:") and saved["server"] != value else ""
    note = f" It no longer always includes {catalog.name(old)}." if old else ""
    if value == AUTO:
        return "Grounder optimized: Grounder will pick the provider for each search from your question." + note
    if kind == "preset":
        return f"Grounder preset {catalog.name(value)}: Grounder will pick the best provider for it on each search." + note
    if kind == "mode":
        found = catalog.mode_of(value.partition(":")[2])
        return _direct_words(f"{found[0]['name']}'s {found[1]['mode']} mode" if found else catalog.name(value)) + note
    return _direct_words(catalog.name(value)) + note


def _apis_words(states: dict[str, str], catalog: Catalog) -> str:
    """The live-data APIs' settings in words: the ones set to Every call or Never, and that every other is Grounder decides."""
    named = {state: [a["name"] for a in catalog.apis if states.get(a["id"]) == state] for state, _ in LIVE_STATES}
    sets = [f"{LIVE_LABEL[state]}: {', '.join(named[state])}." for state in ("always", "off") if named[state]]
    return " ".join(["Live-data APIs, in every mode.", *sets, "All the others: Grounder decides." if sets else "Each one: Grounder decides."])


def _save_live(catalog: Catalog, saved: dict[str, Any], wanted: dict[str, str]) -> dict[str, str]:
    """Save the live-data APIs' settings with ``wanted`` (``{api id: setting}``) laid over what is saved, and return them. Every call goes to ``live_data_always`` and Never to
    ``live_data_off``, each in Grounder's order, a list left empty is removed, and an ``api:<id>`` server saved by an earlier version, which these settings now carry, is cleared."""
    states = {**_live_states(saved, catalog), **wanted}
    changes: dict[str, Any] = {api.LIVE_ALWAYS_KEY: [a["id"] for a in catalog.apis if states[a["id"]] == "always"],
                               api.LIVE_OFF_KEY: [a["id"] for a in catalog.apis if states[a["id"]] == "off"]}
    if saved["server"].startswith("api:"):
        changes["server"] = None
    _save(**changes)
    return states


def _states_from(answered: list[str], catalog: Catalog, saved: dict[str, Any]) -> dict[str, str]:
    """The settings a page two answer names: each API's one checked setting. An API with none checked, or more than one, is left as it is."""
    states = _live_states(saved, catalog)
    for a in catalog.apis:
        checked = [state for state, _ in LIVE_STATES if f"{a['id']}:{state}" in answered]
        if len(checked) == 1:
            states[a["id"]] = checked[0]
    return states


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
        states = _states_from(answers[APIS], catalog, saved)
        if states != _live_states(saved, catalog):
            changes.update({api.LIVE_ALWAYS_KEY: [i for i, state in states.items() if state == "always"], api.LIVE_OFF_KEY: [i for i, state in states.items() if state == "off"]})
            if saved["server"].startswith("api:"):
                changes.setdefault("server", None)  # an older version's API server is Every call now, in the list above
            words.append(_apis_words(states, catalog))
    if not changes:
        return "Nothing changed."
    _save(**changes)
    return " ".join(words)


def _preset_text(preset: dict[str, Any], catalog: Catalog) -> str:
    """A preset's description, what a preset does (``PRESET_NOTE``), and where Grounder says which providers and modes it uses, how many of each (a list of names would be most
    of the catalog). The same words on the form's row and in the step."""
    values = catalog.includes(preset)
    owners = len(dict.fromkeys(value.partition(":")[2].partition(":")[0] for value in values))
    modes = sum(value.startswith("mode:") for value in values)
    uses = f" Uses {owners} provider{'s' if owners != 1 else ''}" + (f" and {modes} mode{'s' if modes != 1 else ''}" if modes else "") + "."
    return " ".join(part for part in (preset["description"], PRESET_NOTE) if part) + (uses if values else "")


def _names(names: list[str]) -> str:
    """Names in a row: all of them to three, else the first three and how many more."""
    if len(names) > 3:
        return f"{', '.join(names[:3])} and {len(names) - 3} more"
    return f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else ", ".join(names)


def _preset_row(preset: dict[str, Any], catalog: Catalog) -> str:
    """A preset's row: its purpose in one line, then how many providers it uses where Grounder says which (an older API does not, so the purpose stands alone). The count is of the
    providers the preset's ``includes`` names that Grounder lists, from the same data the form's lights read."""
    count = len(dict.fromkeys(value.partition(":")[2].partition(":")[0] for value in catalog.includes(preset)))
    return " · ".join(part for part in (_purpose(preset["description"]), f"{count} provider{'s' if count != 1 else ''}" if count else "") if part)


def _snapshot(saved: dict[str, Any]) -> tuple:
    """What ``/search`` can change, to tell at Done whether it did."""
    return saved["preset"], saved["server"], saved["server_mode"], tuple(sorted(saved["off"])), tuple(sorted(saved["always"]))


def _apis_counts(states: dict[str, str]) -> str:
    """The live-data APIs by setting, for the hub's row: ``23 on Grounder decides, 1 on Every call, 1 on Never`` (a setting no API has is left out)."""
    return ", ".join(f"{n} on {LIVE_LABEL[state]}" for state in ("auto", "always", "off") if (n := sum(1 for s in states.values() if s == state)))


def _hub_rows(catalog: Catalog, saved: dict[str, Any]) -> list[dict[str, str]]:
    """The hub: Let Grounder pick, A preset, A provider, Live-data APIs (where Grounder lists any) and Done, the row for what is set now marked "(current)" and saying which."""
    current = _current_pick(saved, catalog)
    kind = current.partition(":")[0]
    here = lambda label, on: f"{label}: {catalog.name(current)}{CURRENT}" if on else label
    rows = [{"value": AUTO, "label": f"Let Grounder pick{CURRENT if current == AUTO else ''}", "description": HUB_NOTE},
            {"value": "presets", "label": here("A preset", kind == "preset"), "description": ", ".join(p["name"] for p in catalog.presets)},
            {"value": "providers", "label": here("A provider", kind in ("provider", "mode")),
             "description": f"{_names([p['name'] for p in catalog.providers])}, each with its modes"}]
    if catalog.apis:
        rows.append({"value": APIS, "label": "Live-data APIs", "description": _apis_counts(_live_states(saved, catalog))})
    return [*rows, {"value": DONE, "label": "Done"}]


def _step_presets(ctx, catalog: Catalog, saved: dict[str, Any]) -> None:
    """The presets, each with its purpose and the providers it uses, then "← Back". A pick is saved and the hub comes back."""
    rows = [{"value": f"preset:{p['id']}", "label": f"{p['name']}{CURRENT if p['id'] == saved['preset'] else ''}", "description": _preset_row(p, catalog)} for p in catalog.presets]
    picked, _ = _ask_paged(ctx, "Which Grounder preset?", rows, current=f"preset:{saved['preset']}")
    if picked and picked != _RETURN:
        _save(**_pick_changes(picked))


def _step_modes(ctx, owner: dict[str, Any], current: str) -> Optional[str]:
    """A provider's modes: Default (its standard search), each mode with a short description, "← Back". The pick, or None for Back."""
    rows = []
    for m in owner["modes"]:
        value = f"mode:{m['id']}"
        rows.append({"value": value, "label": m["mode"] + (CURRENT if current == value else ""), "description": m["description"]})
    value = f"provider:{owner['id']}"
    default = {"value": value, "label": "Default" + (CURRENT if current == value else ""), "description": (owner.get("default_description") or owner["description"]).rstrip(".")}
    picked, _ = _ask_paged(ctx, f"Which {owner['name']} mode?", rows, keep=(default,), current=current)
    return None if picked in (None, _RETURN) else picked


def _step_providers(ctx, catalog: Catalog, saved: dict[str, Any]) -> None:
    """The providers, each with its mode count, then "← Back"; a provider with modes opens its modes, whose Back returns here. A pick is saved and the hub comes back."""
    current = _current_pick(saved, catalog)
    held = current.partition(":")[2].partition(":")[0] if current.partition(":")[0] in ("provider", "mode") else ""
    page = None  # the first time, the page that holds the provider set now; after a provider's modes, the page it was picked on
    while True:
        rows = [{"value": f"provider:{p['id']}", "label": f"{p['name']}{CURRENT if p['id'] == held else ''}",
                 "description": f"{len(p['modes'])} mode{'s' if len(p['modes']) != 1 else ''}" if p["modes"] else ""} for p in catalog.providers]
        picked, page = _ask_paged(ctx, "Which provider should Grounder direct search with?", rows, page=page, current=f"provider:{held}")
        owner = catalog.provider(picked.partition(":")[2]) if picked and picked != _RETURN else None
        if owner is None:
            return
        picked = _step_modes(ctx, owner, current) if owner["modes"] else picked
        if picked:
            _save(**_pick_changes(picked))
            return


def _step_api(ctx, catalog: Catalog, ident: str) -> None:
    """One API's three settings, the current one marked, then "← Back". A pick is saved and the list comes back."""
    name, now = catalog.name(f"api:{ident}"), _live_state(api.settings(), ident)
    rows = [{"value": state, "label": f"{LIVE_LABEL[state]}{CURRENT if state == now else ''}", "description": LIVE_NOTES[state].format(name=name)} for state in ("always", "auto", "off")]
    picked, _ = _ask_paged(ctx, f"How should Grounder use {name}?", rows)
    if picked in LIVE_LABEL:
        _save_live(catalog, api.settings(), {ident: picked})


def _step_apis(ctx, catalog: Catalog, leave: str = BACK_LABEL) -> None:
    """The live-data APIs: "Set all to Grounder decides", one row per API saying its setting now, then ``leave`` ("← Back" from the hub, "Done" from ``/search apis``). An API opens its
    three settings and the list comes back, to the page it was on. Each change is saved at once."""
    page = None
    for _ in range(MAX_TOGGLES):
        states = _live_states(api.settings(), catalog)
        rows = [{"value": a["id"], "label": a["name"], "description": LIVE_LABEL[states[a["id"]]]} for a in catalog.apis]
        picked, page = _ask_paged(ctx, "Live-data APIs: pick one to change how Grounder uses it.", rows, keep=({"value": _ALL, "label": f"Set all to {LIVE_LABEL['auto']}"},),
                                  page=page, up=leave)
        if picked in (None, _RETURN):
            return
        if picked == _ALL:
            _save_live(catalog, api.settings(), {a["id"]: "auto" for a in catalog.apis})
        else:
            _step_api(ctx, catalog, picked)


def _summary_line(saved: dict[str, Any], catalog: Catalog) -> str:
    """What is set now, in one line: ``Grounder: preset Fast · Live-data APIs: Kalshi every call``. A live-data API on Grounder decides is left out."""
    current = _current_pick(saved, catalog)
    kind = current.partition(":")[0]
    lead = "optimized" if current == AUTO else f"preset {catalog.name(current)}" if kind == "preset" else f"direct {catalog.name(current)}"
    states = _live_states(saved, catalog)
    named = [f"{a['name']} {'every call' if states[a['id']] == 'always' else 'never'}" for a in catalog.apis if states[a["id"]] != "auto"]
    return " · ".join([f"Grounder: {lead}", *([f"Live-data APIs: {', '.join(named)}"] if named else [])])


def _hub(ctx, catalog: Catalog) -> tuple[bool, Optional[str]]:
    """``/search`` with no argument where there is no form: the hub, asked again after every screen, until Done. ``(False, None)`` when nothing could ask here (the caller lists what there
    is); else ``(True, reply)``. Done, or a hub the user leaves, answers with the summary line if something changed ("Nothing changed." if not). Each change is saved when it is made."""
    start = _snapshot(api.settings())
    for turn in range(MAX_TOGGLES):
        saved = api.settings()
        picked = ctx.ask_choice(PICK_QUESTION, _hub_rows(catalog, saved))
        if picked is None and not turn:
            return False, None
        if picked in (None, DONE):
            break
        if picked == AUTO:
            _save(**_pick_changes(AUTO))
        elif picked == "presets":
            _step_presets(ctx, catalog, saved)
        elif picked == "providers":
            _step_providers(ctx, catalog, saved)
        elif picked == APIS:
            _step_apis(ctx, catalog)
    now = api.settings()
    return True, (_summary_line(now, catalog) if _snapshot(now) != start else "Nothing changed.")


def _listing(catalog: Catalog, usage: str, saved: dict[str, Any]) -> str:
    lines = [f"{OPTIMIZED_HEADER} (`/search optimized`, the default): {AUTO_LABEL}.",
             f"{PRESETS_HEADER} (`/search <preset>`):", *(f"  {p['id']} ({p['name']}): {p['description']}" for p in catalog.presets),
             f"{PROVIDERS_HEADER} (`/search <provider>` or `/search <provider>:<mode>`, the only search provider called), each provider with its modes:",
             *(f"  {p['id']} ({p['name']})" + (f": {', '.join(m['mode'] for m in p['modes'])}" if p["modes"] else "") for p in catalog.providers)]
    if catalog.apis:
        lines += ["Live-data APIs, each Every call, Never or Grounder decides (`/search apis <id>=every|never|auto`), in every mode:",
                  *(f"  {a['id']} ({a['name']}): {LIVE_LABEL[_live_state(saved, a['id'])]}" for a in catalog.apis)]
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


def search_command(ctx, raw_args: str) -> Optional[str]:
    """``/search [<id>|optimized|apis]``: the hub (or one form, on a Hermes that has one) for the mode and the live-data APIs, or an argument that skips it. ``auto`` is ``optimized``."""
    catalog, problem = _catalog()
    if catalog is None:
        return problem
    saved = api.settings()
    usage = ("Use `/search <id>` to choose a Grounder preset or the one search provider to call (Grounder direct; `/search <provider>:<mode>` for one of its modes), "
             "`/search optimized` to let Grounder pick the provider, or `/search apis` to set each live-data API to Every call, Never or Grounder decides.")
    arg = (raw_args or "").strip().lower()
    if not arg:
        if _has_form(ctx):
            return _form(ctx, catalog, saved, _pages(catalog, saved))
        asked, reply = _hub(ctx, catalog)
        return reply if asked else _listing(catalog, usage, saved)
    if arg == APIS:
        if _has_form(ctx):
            return _form(ctx, catalog, saved, [{**_apis_page(catalog, saved), "optional": False, "hint": APIS_ALONE_HINT}])
        _step_apis(ctx, catalog, leave="Done")
        return _apis_words(_live_states(api.settings(), catalog), catalog)
    if arg.startswith(f"{APIS} "):
        return _apis_argument(arg[len(APIS):].split(), catalog, saved, usage)
    pick = AUTO if arg in (AUTO, OPTIMIZED) else _match(arg, catalog)[0]
    if pick is None:
        return f"{_match(arg, catalog)[1]}\n{_listing(catalog, usage, saved)}"
    if pick.startswith("api:"):  # a live-data API typed as a pick is Every call for it, and leaves the mode alone
        ident = pick.partition(":")[2]
        return _apis_words(_save_live(catalog, saved, {ident: "always"}), catalog)
    _save(**_pick_changes(pick))
    return _pick_words(pick, catalog, saved)


def _apis_argument(tokens: list[str], catalog: Catalog, saved: dict[str, Any], usage: str) -> str:
    """``/search apis <id>=<setting> ...``: set live-data APIs without the page. A setting is ``every`` (Every call), ``never`` or ``auto`` (Grounder decides); the id or the
    name of an API, ``=`` or ``:``, and a setting. Every pair is checked before any is saved."""
    wanted: dict[str, str] = {}
    for token in tokens:
        name, _, setting = token.replace(":", "=", 1).partition("=")
        found = next((a for a in catalog.apis if name in (a["id"].lower(), a["name"].lower())), None)
        if found is None:
            return f"Grounder has no live-data API called {name}.\n{_listing(catalog, usage, saved)}"
        if setting not in LIVE_ANSWERS:
            return f"`{token}` is not a setting: use `{name}=every`, `{name}=never` or `{name}=auto` (Grounder decides)."
        wanted[found["id"]] = LIVE_ANSWERS[setting]
    return _apis_words(_save_live(catalog, saved, wanted), catalog)


def register_commands(ctx) -> None:
    ctx.register_command(
        "search", lambda raw_args: search_command(ctx, raw_args), args_hint="[optimized|preset|provider|provider:mode|apis]",
        description="Choose how Grounder searches: Grounder optimized, a Grounder preset, or Grounder direct (one provider and its mode), and set each live-data API to "
                    "Every call, Never or Grounder decides (on Slack: /hermes search)")
