# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""``/search``, the plugin's one command: choose how Grounder searches.

Grounder has three modes, and ``/search`` chooses one: **Grounder optimized** (Grounder picks the provider for the user's question; the default), a **Grounder preset**
(Fast, Cheap, Live, Deep, Code or Balanced: Grounder picks the best provider for that preset) or **Grounder pinned** (the user picks the provider, and optionally one of
its modes: that provider is the only search provider called, and what it finds is what the user gets, even when that is nothing). Every mode returns the provider's own results, each uncut and in its own
order. ``/search`` also sets each live-data API, in every mode, to **Every call**, **Never** or **Grounder decides** (the default: asked only when the question's features call for it).
Every Hermes today asks with ``ctx.ask_choice``, and ``/search`` opens a hub, "How should Grounder search?": Let Grounder pick (Grounder optimized), A preset, A provider, Live-data APIs
and Done, the current choice marked "(current)". Each choice opens its own screen (the presets, each with its purpose and the providers it uses; the providers, each with what its
standard search returns and how many modes it has, then one provider's modes; the live-data APIs, then one API's Every call, Grounder decides or Never) and every screen has a "← Back" row that
returns to the hub. A list is one view, every row of it on screen at once, and each row is one line (``ROW_WIDTH``); only a surface that caps a card pages it. A pick closes the menu and is answered
with what is set now, in the shape of Hermes's own "Model switched" reply (``_switched``: the mode, the provider and its mode, what it returns, what happens when it finds nothing, the live-data
APIs, and where it is saved): Let Grounder pick at once, a preset or a provider from its screen, the live-data APIs from theirs once one is changed. That is the command's reply, which every surface
shows (``ctx.post_message`` answers ok in the classic CLI's gateway and shows nothing); Done, or a menu the user leaves, answers "Nothing changed.". A Hermes that offers ``ctx.ask_form`` (none ships it yet: it is a proposed addition to Hermes's plugin surface)
shows the same choices as one form of two pages instead. Typed arguments (``/search exa``, ``/search exa:instant``, ``/search fast``, ``/search auto`` or ``/search optimized``,
``/search apis`` or ``/search apis espn=every mlb=never``) skip the hub. On Slack ``/search`` is Slack's own command, so ``/hermes search`` runs this one.

A preset and a pinned provider clear each other. The choice lives on the user's Grounder account, the one setting grounder.net's Routing page shows (``provider.read_account`` and
``save_account``, ``GET`` and ``PUT /v1/search/settings``): ``/search`` reads it when it opens, writes every choice to it as it is made and takes what Grounder answers as the new state, so a
change made on either side is what the other shows, and nothing is kept in Hermes's config. A read or write that fails is answered with its reason and changes nothing. A search made with a key
or a sign-in sends none of the choices: Grounder applies the saved setting to a search that sends no mode, preset or provider of its own, and bills it as that mode (``provider._send``), and
what the model passes to ``grounder_search`` applies to that search alone. A pinned provider is the only search provider a search calls; ``/search`` saves it with its fallback off, so when it
finds nothing the search returns nothing (a provider already saved with its fallback on keeps it, and the words say Grounder then picks one other). The live-data APIs set to Never or Every call
are the account's too, so one Grounder adds later is Grounder decides. Typed ``/search api:<id>`` sets one API to Every call. The lists of choices are Grounder's public ones, fetched when a command
runs and never kept, so a new provider, mode, preset or API is on offer as soon as Grounder lists it. With no search key and no sign-in a command answers with how to sign in instead of a list.
"""

from __future__ import annotations

import textwrap
from typing import Any, Optional

from . import direct_dev
from . import provider as api

AUTO = "auto"
OPTIMIZED = "optimized"  # typed as an argument, the same as ``auto``
OPTIMIZED_HEADER = "Grounder optimized"
AUTO_LABEL = "Grounder picks the provider for your question"
AUTO_NOTE = "The default. The provider's own results, each uncut and in its own order."
DEV = "dev"  # Grounder direct by itself: the hub row, and typed as ``/search direct`` (or the other words in DEV_WORDS)
DEV_WORDS = (DEV, "direct", "grounder-direct", "direct-dev", "grounder-direct-dev")
ADDON = "addon"  # the hub row and typed word (``/search addon [on|off]``) for Grounder direct as an add-on to the search mode
ADDON_WORDS = (ADDON, "add-on", "direct-addon", "direct-add-on")
ADDON_NOTES = ("either or both",)
DEV_NOTES = ("by itself: only the address you give", "only the address you give", "by itself")
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
# The same three in the few words a card's row has room for, and as the hub's row counts them when the full words do not fit ("23 auto, 1 every call, 1 never").
LIVE_BRIEF = {"always": "on every search, in every mode", "off": "never asked, in any mode", "auto": "when a question needs it"}
LIVE_SHORT = {"always": "every call", "off": "never", "auto": "auto"}
LIVE_ANSWERS = {"every": "always", "always": "always", "never": "off", "off": "off", "auto": "auto", "decides": "auto"}
PRESETS_HEADER, PROVIDERS_HEADER = "Grounder preset", "Grounder pinned"
PICK_QUESTION = "How should Grounder search?"
PRESET_NOTE = "Grounder picks the best provider for this preset."
PIN_NOTE = "The only search provider called."
# The most choices one ask_choice card can hold where a messaging platform caps them and drops the rest without a word, counting what Hermes adds to the card itself: Discord's
# buttons stop at 25 and Hermes keeps one for "Other"; WhatsApp Cloud's list takes 10 rows and Hermes appends an "Other" row after slicing the choices to 10, so the card holds 9 (an
# eleventh row is one WhatsApp refuses). A list is asked whole, one view, everywhere else; where a surface caps the card and the list does not fit it, it is asked a page at a time.
SURFACE_CHOICE_CAPS = {"discord": 24, "whatsapp_cloud": 9}
# The most characters a row takes with its number ("12. Label — note") and still is one line. Hermes's classic CLI wraps a numbered row at 60 characters at most, and at the terminal's
# width less ten where that is narrower, so 56 is one line from a 66-column terminal up. A note that does not fit gives way to a shorter one, and the last is cut at a word.
ROW_WIDTH = 56
_MORE = "__more__"  # the paging row, where a surface caps a card: "More: 8–14 of 22"
_RETURN, _ALL, DONE = "back", "__all__", "done"  # "← Back" (a screen's way up; its value is a word, so typing "back" picks it), "Set all to Grounder decides" and the hub's Done
BACK_LABEL, CURRENT = "← Back", " (current)"
HUB_NOTE = "a provider per question"
UNCHANGED = "Nothing changed."
SAVED_NOTE = "(saved to your Grounder account; applies wherever it searches)"
REPLY_WIDTH = 64  # the most characters a line of a pick's reply takes: Hermes prints a reply as it is, and a terminal breaks a longer line mid-word
# An offering's inputs that narrow a search, as the reply to a pick names them on its "Filters" line; any other input (the query itself, pages to read) is not a filter.
INPUT_WORDS = {"days": "date window", "domains": "sites", "language": "language"}
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
    return ", ".join(_feature_words(offering)).capitalize()


def _feature_words(offering: dict[str, Any]) -> list[str]:
    """What an offering returns, as words: its features, ``web-search`` as "web search". Empty when Grounder lists none."""
    features = offering.get("features")
    return [f.replace("-", " ") for f in features if isinstance(f, str)] if isinstance(features, list) else []


def _filter_words(offering: dict[str, Any]) -> list[str]:
    """What narrows an offering's search, as words (``INPUT_WORDS``), from the inputs Grounder lists for it."""
    inputs = offering.get("inputs")
    return [INPUT_WORDS[i] for i in inputs if isinstance(i, str) and i in INPUT_WORDS] if isinstance(inputs, list) else []


def _providers(rows: Any) -> list[dict[str, Any]]:
    """The providers, each with ``modes``: ``[{"id": "exa:instant", "mode": "instant", "description": "Cache only, the lowest latency"}]``, the offerings that are not its default
    search, and ``default_description``, the default search's own line ("" from an API that has none). A mode's description is the one Grounder lists for it; from an API that lists none
    it is made from the vendor's note on the mode, else its features. The provider and each mode also carry ``features`` and ``filters``, what that search returns and what narrows it,
    as words (empty from an API that lists neither)."""
    out = []
    for row in _rows(rows, "provider"):
        entry = _entry(row, "provider")
        offerings = row.get("offerings") if isinstance(row.get("offerings"), list) else []
        modes: dict[str, dict[str, Any]] = {}
        default, standard = "", None
        for o in offerings:
            if not isinstance(o, dict):
                continue
            mode, said = o.get("mode"), o.get("description")
            said = said.strip() if isinstance(said, str) else ""
            if mode is None:
                default, standard = default or said, standard or o
            elif isinstance(mode, str) and api._ID.match(mode) and mode != DEFAULT_ALIASES.get(entry["id"]):
                modes.setdefault(mode, {"id": f"{entry['id']}:{mode}", "mode": mode, "description": said or _mode_words(o), "features": _feature_words(o), "filters": _filter_words(o)})
        out.append({**entry, "modes": list(modes.values()), "default_description": default, "features": _feature_words(standard or {}), "filters": _filter_words(standard or {})})
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


class Failed(Exception):
    """A read or write of the account's setting that did not happen: the message is the reply."""


class Account:
    """The account's saved search setting while one command runs: read when the command opens, and replaced by what Grounder answers to each write, so no screen reads it again
    (``saved`` is ``provider._local``'s form). A read or write that fails raises ``Failed`` with the reason and leaves ``saved`` as it was."""

    def __init__(self, saved: dict[str, Any]):
        self.saved = saved

    @classmethod
    def read(cls) -> "Account":
        saved, error = api.read_account()
        if saved is None:
            raise Failed(f"Could not read your Grounder search setting: {error}")
        return cls(saved)

    def save(self, **changes: Any) -> None:
        saved, error = api.save_account(self.saved, **changes)
        if saved is None:
            raise Failed(f"Could not save that to your Grounder account, so nothing changed: {error}")
        self.saved = saved


def _cap(ctx) -> Optional[int]:
    """The most choices one card shows on this surface, when it caps them; None where it does not, or where Hermes names no surface."""
    surface = getattr(ctx, "session_surface", None)
    try:
        return SURFACE_CHOICE_CAPS.get(surface()) if callable(surface) else None
    except Exception:  # a Hermes that cannot name the surface shows the whole card, as it did before surfaces were named
        return None


def _page_size(ctx, kept: int, count: int) -> int:
    """How many items one page of a list shows: all ``count`` of them, so a list is one view, except where the surface caps a card and the list does not fit it with its ``kept`` rows
    and Back. There a page holds what the cap leaves beside ``kept``, More and Back."""
    cap = _cap(ctx)
    return max(1, count) if cap is None or kept + count + 1 <= cap else max(1, cap - kept - 2)


def _clip(text: str, room: int) -> str:
    """``text`` in ``room`` characters: itself where it fits, else cut at a word and ended with "…" ("" where only a few characters would be left)."""
    if len(text) <= room:
        return text
    if room < 6:
        return ""
    cut = text[:room - 1]
    head = cut.rpartition(" ")[0]
    return (head if len(head) >= room // 2 else cut).rstrip(" ,;:·(-") + "…"


def _card(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One card's rows as ``ask_choice`` takes them, each one line. Hermes draws a row as its number, the label and, after " — ", the description, so a row has ROW_WIDTH characters less
    its number. ``notes`` are what the row could say beside its label, longest first: the first that fits is the description, else the last cut at a word where a few words of it
    still fit, else none. ``tail`` (" (current)") is kept whole when a label is too long for its row, which cuts the name before it."""
    card = []
    for n, row in enumerate(rows, 1):
        room = ROW_WIDTH - len(f"{n}. ")
        tail = row.get("tail") or ""
        label = _clip(row["label"], room - len(tail)) + tail
        notes = [note for note in row.get("notes") or () if note]
        fits = room - len(label) - 3
        note = next((note for note in notes if len(note) <= fits), _clip(notes[-1], fits) if notes and fits >= 16 else "")
        card.append({"value": row["value"], "label": label, **({"description": note} if note else {})})
    return card


def _ask_paged(ctx, question: str, items: list[dict[str, Any]], keep: tuple = (), page: Optional[int] = None, current: Optional[str] = None,
               up: str = BACK_LABEL) -> tuple[Optional[str], int]:
    """``ctx.ask_choice`` as ``(picked, page it was picked on)``: the ``keep`` rows (such as "Set all to Grounder decides"), the items, then Back, every row one line (``_card``). The
    list is one view. Where a surface caps a card and the list does not fit it (``_page_size``), it is a page at a time instead: ``keep`` on every page, the page's items, ``More: 8–14
    of 22`` where more follow, then Back, which on a later page goes to the page before (``← Back: 1–7 of 22``) and opens on the page that holds ``current`` (a value), else on ``page``,
    else the first. Back on the first page is ``up`` and returns ``_RETURN``. None for ``picked`` when nothing can ask here or the user did not pick."""
    keep = list(keep)
    size = _page_size(ctx, len(keep), len(items))
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
        picked = ctx.ask_choice(question, _card(rows))
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
    """The pick the form starts on: what is saved now, where the page has a row for it. A preset or provider Grounder no longer lists has no row, so the form starts on
    Grounder optimized and (see ``_form``) leaves it saved unless the user picks another."""
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
    """A live-data API's setting now: ``off`` (Never), ``always`` (Every call) or ``auto`` (Grounder decides, the default)."""
    return api._use(saved, ident)


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
    """What a pick saves. A preset and a pinned provider clear each other, and Grounder optimized clears both."""
    kind, _, ident = value.partition(":")
    if kind == "preset":
        return {"preset": ident, "server": None, "server_mode": None}
    if value == AUTO:
        return {"preset": None, "server": None, "server_mode": None}
    if kind == "mode":
        owner, _, mode = ident.partition(":")
        return {"preset": None, "server": f"provider:{owner}", "server_mode": mode}
    return {"preset": None, "server": value, "server_mode": None}


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _apis_counts(states: dict[str, str], brief: bool = False) -> str:
    """The live-data APIs by setting: ``23 on Grounder decides, 1 on Every call, 1 on Never`` (a setting no API has is left out), or ``brief``, for a row with no room for that:
    ``23 auto, 1 every call, 1 never``."""
    said = LIVE_SHORT if brief else {state: f"on {label}" for state, label in LIVE_STATES}
    return ", ".join(f"{n} {said[state]}" for state in ("auto", "always", "off") if (n := sum(1 for s in states.values() if s == state)))


def _preset_uses(preset: dict[str, Any], catalog: Catalog) -> str:
    """How many providers and modes a preset draws from, where Grounder says which: ``2 providers and 1 mode`` ("" from an API that does not say)."""
    values = catalog.includes(preset)
    owners = len(dict.fromkeys(value.partition(":")[2].partition(":")[0] for value in values))
    modes = sum(value.startswith("mode:") for value in values)
    return _count(owners, "provider") + (f" and {_count(modes, 'mode')}" if modes else "") if values else ""


def _block(headline: str, lines: list[str]) -> str:
    """A reply in the shape of Hermes's own "Model switched": the headline, then each line indented under it. A line longer than REPLY_WIDTH is wrapped at a word, its rest
    indented further, so no terminal breaks it mid-word."""
    wrapped = [part for line in lines for part in textwrap.wrap(line, REPLY_WIDTH, initial_indent="  ", subsequent_indent="    ", break_long_words=False, break_on_hyphens=False)]
    return "\n".join([f"✓ {headline}", *wrapped])


def _switched(saved: dict[str, Any], catalog: Catalog, value: str, changed: bool = True) -> str:
    """What a pick answers, in the shape of Hermes's own "Model switched" reply: a headline naming the mode, a line for each thing now set, and where it is saved. Grounder direct says the
    provider (the only search provider called), its mode with the one line Grounder gives it, what that search returns and what narrows it, and what happens when it finds nothing
    (``saved``'s fallback after the write); a preset says what it is for and how many providers it draws from; every mode says the live-data APIs' settings, counted in fewer words
    where the full ones would not fit the line. ``changed`` False says the pick was what the account already had."""
    kind, _, ident = value.partition(":")
    if value == AUTO:
        mode, lines = OPTIMIZED_HEADER, [f"Provider: {AUTO_LABEL}", "Results: the provider's own, each uncut and in its own order"]
    elif kind == "preset":
        preset = next((p for p in catalog.presets if p["id"] == ident), None)
        uses = _preset_uses(preset, catalog) if preset else ""
        mode, lines = PRESETS_HEADER, [f"Preset: {catalog.name(value)}", *([f"For: {preset['description'].rstrip('.')}"] if preset and preset["description"] else []),
                                       "Provider: " + PRESET_NOTE.rstrip("."), *([f"Draws from: {uses}"] if uses else [])]
    else:
        found = catalog.mode_of(ident) if kind == "mode" else None
        owner = found[0] if found else catalog.provider(ident) if kind == "provider" else None
        offering = found[1] if found else owner or {}
        name = owner["name"] if owner else catalog.name(value)
        mode, lines = PROVIDERS_HEADER, [f"Provider: {name}, {PIN_NOTE[0].lower()}{PIN_NOTE[1:].rstrip('.')}"]
        if found:
            lines.append(" · ".join(part for part in (f"Mode: {found[1]['mode']}", found[1]["description"].rstrip(".")) if part))
        elif owner and owner["modes"]:
            lines.append(" · ".join(part for part in ("Mode: Default", (owner.get("default_description") or owner["description"]).rstrip(".")) if part))
        lines += [f"{title}: {', '.join(words)}" for title, words in (("Capabilities", offering.get("features")), ("Filters", offering.get("filters"))) if words]
        lines.append("Results: what it finds; if nothing, Grounder picks one other" if saved["fallback"] else "Results: only what it finds, even when that is nothing")
    if catalog.apis:
        states = _live_states(saved, catalog)  # with the command that sets them where the line has room, in fewer words where the full ones do not fit
        said = [f"Live-data APIs: {_apis_counts(states, brief=brief)}{hint}" for brief in (False, True) for hint in (" (`/search apis`)", "")]
        lines.append(next((line for line in said if len(line) + 2 <= REPLY_WIDTH), said[-1]))
    return _block(f"Search {'switched' if changed else 'unchanged'}: {mode}", [*lines, SAVED_NOTE])


def _pick(account: Account, catalog: Catalog, value: str) -> str:
    """Save a pick to the account and answer with what is set now (``_switched``). A pick the account already shows is still written: the view this command read may be a minute old."""
    was = (account.saved["preset"], account.saved["server"], account.saved["server_mode"])
    account.save(**_pick_changes(value))
    left_dev = direct_dev.is_on()
    if left_dev:  # a search mode leaves Grounder direct by itself; the add-on, when on, stays on
        direct_dev.set_state(direct_dev.OFF)
    now = account.saved
    return _with_direct(_switched(now, catalog, value, changed=left_dev or (now["preset"], now["server"], now["server_mode"]) != was))


DIRECT_LINE = "  Grounder direct: a site you name is read straight from it, as you; otherwise this mode searches"
DIRECT_QUESTION = "Also read sites you name directly, as you?"


def _with_direct(reply: str) -> str:
    """A search mode's reply, saying "Grounder direct + " in its headline and what that does when Grounder direct is on with it, and neither when it is not."""
    head, _, rest = reply.partition("\n")
    head = head.replace("Grounder direct + ", "", 1)
    rest = rest.replace(DIRECT_LINE + "\n", "", 1)
    if not direct_dev.addon_on():
        return f"{head}\n{rest}" if rest else head
    head = head.replace("switched: ", "switched: Grounder direct + ", 1).replace("unchanged: ", "unchanged: Grounder direct + ", 1)
    return f"{head}\n{DIRECT_LINE}\n{rest}"


def _ask_direct(ctx) -> None:
    """After a search mode is picked in the hub: whether Grounder direct goes with it. Yes reads a site the user names straight from it, as them (their Safari's
    user agent and headers, and a sign-in they saved for it); No searches only. A question the user leaves changes nothing."""
    on = direct_dev.addon_on()
    rows = [{"value": "direct-yes", "label": "Yes", "tail": CURRENT if on else "", "notes": ("sites you name, read directly as you", "read directly as you")},
            {"value": "direct-no", "label": "No", "tail": "" if on else CURRENT, "notes": ("search only",)}]
    picked = ctx.ask_choice(DIRECT_QUESTION, _card(rows))
    if picked in ("direct-yes", "direct-no"):
        direct_dev.set_state(direct_dev.ADDON if picked == "direct-yes" else direct_dev.OFF)


def _mode_label(saved: dict[str, Any], catalog: Catalog) -> str:
    """The account's search mode in a few words: ``Grounder optimized``, ``Grounder preset Fast`` or ``Grounder pinned Exa instant``."""
    current = _current_pick(saved, catalog)
    kind = current.partition(":")[0]
    if current == AUTO:
        return OPTIMIZED_HEADER
    return f"{PRESETS_HEADER if kind == 'preset' else PROVIDERS_HEADER} {catalog.name(current)}"


def _set_addon(account: Account, catalog: Catalog, on: bool) -> str:
    """Turn Grounder direct on or off as an add-on to the search mode, on this Mac only. On, it replaces Grounder direct by itself, and the account's mode searches again."""
    was = direct_dev.state()
    direct_dev.set_state(direct_dev.ADDON if on else direct_dev.OFF)
    mode = _mode_label(account.saved, catalog)
    changed = was != direct_dev.state()
    if not on:
        return _block(f"Search {'switched' if changed else 'unchanged'}: {mode}", [f"Search: {mode}", "Grounder direct: off; pages are read by Grounder's reader"])
    version = direct_dev.safari_version()
    return _block(f"Search {'switched' if changed else 'unchanged'}: Grounder direct + {mode}", [
        f"A site you name: Grounder direct reads it straight from the site on this Mac" + (f", as your Safari {version} asks for it" if version else ""),
        f"No site named: {mode} searches",
        "Both: when a question needs the site and wider context, the model uses both and says which part came from where",
        "No fallback: a page that cannot be read says why",
        "Credits: a direct read uses no search provider credits and no Grounder credits",
        "(saved on this machine: web.grounder.direct in config.yaml)"])


def _pick_dev(account: Account, catalog: Catalog) -> str:
    """Turn Grounder direct on by itself, on this machine only, and answer with what it does. The account's own setting is left as it is, and applies again at the next pick."""
    was = direct_dev.is_on()
    direct_dev.set_on(True)
    current = _current_pick(account.saved, catalog)
    kind = current.partition(":")[0]
    kept = OPTIMIZED_HEADER if current == AUTO else f"{PRESETS_HEADER if kind == 'preset' else PROVIDERS_HEADER}: {catalog.name(current)}"
    version = direct_dev.safari_version()
    signed = direct_dev.sites()
    lines = ["Goes to: only the address you give, read straight from its site on this Mac",
             f"Asks as: your Safari {version} (its user agent and headers), in a fresh session" if version else "Asks as: nothing; Safari is not installed here",
             (f"Sign-ins (optional): signed in on {', '.join(site for site, _ in signed)}; every other site is read logged out" if signed
              else "Sign-ins (optional): none needed; every site is read logged out"),
             "(to read a site signed in: /search-login <site>; /search-logout <site> forgets it)",
             "Search: none. No search provider is called, and nothing else is tried when a page cannot be read",
             "Credits: none; no search provider credits and no Grounder credits",
             f"Your account: keeps {kept}, which applies again when you pick another mode",
             "(saved on this machine: web.grounder.direct in config.yaml)"]
    return _block(f"Search {'unchanged' if was else 'switched'}: {direct_dev.NAME}", lines)


def _apis_words(states: dict[str, str], catalog: Catalog) -> str:
    """The live-data APIs' settings, in the shape of ``_switched``: the ones set to Every call or Never, that every other is Grounder decides, and where it is saved."""
    named = {state: [a["name"] for a in catalog.apis if states.get(a["id"]) == state] for state, _ in LIVE_STATES}
    sets = [f"{LIVE_LABEL[state]}: {', '.join(named[state])}" for state in ("always", "off") if named[state]]
    return _block("Live-data APIs set, in every mode", [*sets, f"{LIVE_LABEL['auto']}: {'all the others' if sets else 'each one'}", SAVED_NOTE])


def _live_changes(saved: dict[str, Any], catalog: Catalog, states: dict[str, str]) -> dict[str, list[str]]:
    """What to save for the live-data APIs at ``states`` (``{api id: setting}`` for the ones Grounder lists): Every call as ``always`` and Never as ``off``, each in Grounder's order, and
    an API the account has saved that the list does not show left as it is, which a write that named only the listed ones would send back to Grounder decides."""
    listed = {a["id"] for a in catalog.apis}
    return {name: [i for i in saved[name] if i not in listed] + [a["id"] for a in catalog.apis if states[a["id"]] == state]
            for name, state in (("always", "always"), ("off", "off"))}


def _save_live(account: Account, catalog: Catalog, wanted: dict[str, str]) -> dict[str, str]:
    """Save the live-data APIs' settings with ``wanted`` (``{api id: setting}``) laid over the account's, and return them as Grounder answered."""
    account.save(**_live_changes(account.saved, catalog, {**_live_states(account.saved, catalog), **wanted}))
    return _live_states(account.saved, catalog)


def _states_from(answered: list[str], catalog: Catalog, saved: dict[str, Any]) -> dict[str, str]:
    """The settings a page two answer names: each API's one checked setting. An API with none checked, or more than one, is left as it is."""
    states = _live_states(saved, catalog)
    for a in catalog.apis:
        checked = [state for state, _ in LIVE_STATES if f"{a['id']}:{state}" in answered]
        if len(checked) == 1:
            states[a["id"]] = checked[0]
    return states


def _form(ctx, catalog: Catalog, account: Account, pages: list[dict[str, Any]]) -> str:
    """Show the pages as one form and write what came back to the account, in one write."""
    answers = ctx.ask_form(pages)
    if answers is None:
        return UNCHANGED
    saved = account.saved
    changes: dict[str, Any] = {}
    pick = answers.get("pick")
    # Page one starts on what is saved, and counts as answered from the start: a pick equal to that is the page left alone, which must not clear
    # a pin it cannot show (one Grounder no longer lists), so only a different pick saves.
    picked = bool(pick) and pick != _current_pick(saved, catalog)
    if picked:
        changes.update(_pick_changes(pick))
    live = False
    if isinstance(answers.get(APIS), list):
        states = _states_from(answers[APIS], catalog, saved)
        live = states != _live_states(saved, catalog)
        if live:
            changes.update(_live_changes(saved, catalog, states))
    if not changes:
        return UNCHANGED
    account.save(**changes)
    return "\n".join([*([_switched(account.saved, catalog, pick)] if picked else []), *([_apis_words(_live_states(account.saved, catalog), catalog)] if live else [])])


def _preset_text(preset: dict[str, Any], catalog: Catalog) -> str:
    """A preset's description, what a preset does (``PRESET_NOTE``), and where Grounder says which providers and modes it uses, how many of each (a list of names would be most
    of the catalog). The form's row."""
    uses = _preset_uses(preset, catalog)
    return " ".join(part for part in (preset["description"], PRESET_NOTE) if part) + (f" Uses {uses}." if uses else "")


def _names(names: list[str], shown: int = 3) -> str:
    """Names in a row: all of them to ``shown``, else the first ``shown`` and how many more."""
    if len(names) > shown:
        return f"{', '.join(names[:shown])} and {len(names) - shown} more"
    return f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else ", ".join(names)


def _brief(text: str) -> str:
    """A description's first clause (``_purpose``), and of that what comes before ", and": the few words a row has room for."""
    return _purpose(text).partition(", and ")[0]


def _preset_row(preset: dict[str, Any], catalog: Catalog, brief: bool = False) -> str:
    """A preset's row: its purpose in one line (``brief``: in fewer words), then how many providers it uses where Grounder says which (an older API does not, so the purpose stands
    alone). The count is of the providers the preset's ``includes`` names that Grounder lists, from the same data the form's lights read."""
    count = len(dict.fromkeys(value.partition(":")[2].partition(":")[0] for value in catalog.includes(preset)))
    return " · ".join(part for part in ((_brief if brief else _purpose)(preset["description"]), _count(count, "provider") if count else "") if part)


def _provider_notes(owner: dict[str, Any]) -> tuple[str, ...]:
    """What a provider's row could say beside its name, longest first: what its standard search returns and how many modes it has, then each alone."""
    features, modes = ", ".join(owner.get("features") or []), _count(len(owner["modes"]), "mode") if owner["modes"] else ""
    return tuple(note for note in (" · ".join(part for part in (features, modes) if part), features, modes) if note)


def _hub_rows(catalog: Catalog, saved: dict[str, Any]) -> list[dict[str, str]]:
    """The hub: Let Grounder pick, A preset, A provider, Live-data APIs (where Grounder lists any) and Done, each one line (``_card``). The row for what is set now is marked
    "(current)" and says which; the others say what they hold in as many names as fit."""
    now = direct_dev.state()
    current = DEV if now == direct_dev.ONLY else _current_pick(saved, catalog)
    kind = current.partition(":")[0]
    presets, providers = [p["name"] for p in catalog.presets], [p["name"] for p in catalog.providers]

    def row(value: str, label: str, on: bool, count: str, *notes: str) -> dict[str, Any]:
        return {"value": value, "label": f"{label}: {catalog.name(current)}", "tail": CURRENT, "notes": (count,)} if on else {"value": value, "label": label, "notes": (*notes, count)}

    rows = [{"value": AUTO, "label": OPTIMIZED_HEADER, "tail": CURRENT if current == AUTO else "", "notes": (HUB_NOTE, "picks per question")},
            row("presets", "Grounder presets", kind == "preset", _count(len(presets), "preset"), ", ".join(presets), _names(presets)),
            row("providers", PROVIDERS_HEADER, kind in ("provider", "mode"), _count(len(providers), "provider"), _names(providers), _names(providers, 2), _names(providers, 1)),
            {"value": DEV, "label": "Grounder direct", "tail": CURRENT if current == DEV else "", "notes": DEV_NOTES}]
    if now == direct_dev.ADDON:  # the mode set now says Grounder direct goes with it
        for r in rows[:3]:
            if r.get("tail") == CURRENT:
                r["notes"] = ("+ Grounder direct",)
    if catalog.apis:
        states = _live_states(saved, catalog)
        rows.append({"value": APIS, "label": "Live-data APIs", "notes": (_apis_counts(states), _apis_counts(states, brief=True))})
    rows.append({"value": SETTINGS, "label": "Settings", "notes": (f"resolver: {RESOLVER_SHORT[direct_dev.resolver()]}",)})
    return _card([*rows, {"value": DONE, "label": "Done"}])


def _step_presets(ctx, account: Account, catalog: Catalog) -> Optional[str]:
    """The presets, each with its purpose and the providers it uses, then "← Back". A pick is saved and is the reply (``_pick``), so the menu closes; None for Back, which asks the
    hub again; a screen the user leaves changes nothing."""
    saved = account.saved
    rows = [{"value": f"preset:{p['id']}", "label": p["name"], "tail": CURRENT if p["id"] == saved["preset"] else "",
             "notes": (_preset_row(p, catalog), _preset_row(p, catalog, brief=True), _brief(p["description"]))} for p in catalog.presets]
    picked, _ = _ask_paged(ctx, "Which Grounder preset?", rows, current=f"preset:{saved['preset']}")
    if picked == _RETURN:
        return None
    return _pick(account, catalog, picked) if picked else UNCHANGED


def _step_modes(ctx, owner: dict[str, Any], current: str) -> Optional[str]:
    """A provider's modes: Default (its standard search), each mode with the line Grounder gives it, "← Back". The pick, ``_RETURN`` for Back, or None for a screen the user left."""
    rows = [{"value": f"mode:{m['id']}", "label": m["mode"], "tail": CURRENT if current == f"mode:{m['id']}" else "", "notes": (m["description"].rstrip("."),)} for m in owner["modes"]]
    value = f"provider:{owner['id']}"
    default = {"value": value, "label": "Default", "tail": CURRENT if current == value else "", "notes": ((owner.get("default_description") or owner["description"]).rstrip("."),)}
    picked, _ = _ask_paged(ctx, f"Which {owner['name']} mode?", rows, keep=(default,), current=current)
    return picked


def _step_providers(ctx, account: Account, catalog: Catalog) -> Optional[str]:
    """The providers in one list, each with what its standard search returns and how many modes it has, then "← Back"; a provider with modes opens its modes, whose Back returns here.
    A pick is saved and is the reply (``_pick``), so the menu closes; None for Back, which asks the hub again; a screen the user leaves changes nothing."""
    current = _current_pick(account.saved, catalog)
    held = current.partition(":")[2].partition(":")[0] if current.partition(":")[0] in ("provider", "mode") else ""
    page = None  # where a surface pages the list: the first time, the page that holds the provider set now; after a provider's modes, the page it was picked on
    while True:
        rows = [{"value": f"provider:{p['id']}", "label": p["name"], "tail": CURRENT if p["id"] == held else "", "notes": _provider_notes(p)} for p in catalog.providers]
        picked, page = _ask_paged(ctx, "Which provider should Grounder pin?", rows, page=page, current=f"provider:{held}")
        if picked == _RETURN:
            return None
        owner = catalog.provider(picked.partition(":")[2]) if picked else None
        if owner is None:
            return UNCHANGED
        if owner["modes"]:
            picked = _step_modes(ctx, owner, current)
            if picked == _RETURN:
                continue
            if picked is None:
                return UNCHANGED
        return _pick(account, catalog, picked)


def _step_api(ctx, account: Account, catalog: Catalog, ident: str) -> None:
    """One API's three settings, the current one marked, then "← Back". A pick is saved and the list comes back."""
    name, now = catalog.name(f"api:{ident}"), _live_state(account.saved, ident)
    rows = [{"value": state, "label": LIVE_LABEL[state], "tail": CURRENT if state == now else "", "notes": (LIVE_BRIEF[state],)} for state in ("always", "auto", "off")]
    picked, _ = _ask_paged(ctx, f"How should Grounder use {name}?", rows)
    if picked in LIVE_LABEL:
        _save_live(account, catalog, {ident: picked})


SETTINGS = "settings"
SETTINGS_QUESTION = "Grounder direct settings"
RESOLVER_QUESTION = "How should Grounder direct look sites up?"
RESOLVER_LABEL = {"system": "Your own resolver", "grounder": "Grounder's resolver"}
RESOLVER_SHORT = {"system": "your own", "grounder": "Grounder's"}
RESOLVER_NOTES = {"system": ("the default: as the rest of this Mac does", "as the rest of this Mac does", "the default"),
                  "grounder": ("your network's filter does not block reads", "not blocked by your filter")}
RESOLVER_LINES = {"system": ["Reads look sites up the way the rest of this Mac does, through any filter you or your network set"],
                  "grounder": ["Reads look sites up through Grounder, so a filter on this Mac or its network does not block them",
                               "Grounder sees which sites are read, not what is on them; pages still go straight from this Mac to each site"]}


def _resolver_reply(value: str, was: str) -> str:
    return _block(f"Resolver {'switched' if value != was else 'unchanged'}: {RESOLVER_LABEL[value]}",
                  [*RESOLVER_LINES[value], "(saved on this machine: web.grounder.direct_resolver in config.yaml)"])


def _set_resolver(value: str) -> str:
    was = direct_dev.resolver()
    direct_dev.set_resolver(value)
    from . import direct_formats
    direct_formats.forget_cached_searches()
    return _resolver_reply(value, was)


def _step_settings(ctx) -> Optional[str]:
    """Settings: Grounder direct's own settings, saved on this machine. Today one, the resolver reads look sites up with; it opens its two choices, the current
    one marked, then "← Back". A pick is saved and is the reply; None for Back from Settings, which asks the hub again."""
    while True:
        now = direct_dev.resolver()
        rows = [{"value": "resolver", "label": "Resolver", "notes": (RESOLVER_LABEL[now],)}]
        picked, _ = _ask_paged(ctx, SETTINGS_QUESTION, rows)
        if picked != "resolver":
            return None if picked == _RETURN else UNCHANGED
        choices = [{"value": v, "label": RESOLVER_LABEL[v], "tail": CURRENT if v == now else "", "notes": RESOLVER_NOTES[v]} for v in direct_dev.RESOLVERS]
        chosen, _ = _ask_paged(ctx, RESOLVER_QUESTION, choices)
        if chosen == _RETURN:
            continue
        return _set_resolver(chosen) if chosen in direct_dev.RESOLVERS else UNCHANGED


def _step_apis(ctx, account: Account, catalog: Catalog, hub: bool = False) -> Optional[str]:
    """The live-data APIs in one list: "Set all to Grounder decides", one row per API saying its setting now, then the way out. An API opens its three settings and the list comes
    back (to the page it was on, where a surface pages it), since several are usually set in one go; each change is saved at once. The way out is the reply, the settings in words
    (``_apis_words``), so the menu closes. From the hub (``hub``) it is "← Back" until a setting differs from when the screen opened and "Done" from then: Back returns None, which
    asks the hub again, and a screen left with nothing changed says so."""
    start = _live_states(account.saved, catalog)
    page = None
    for _ in range(MAX_TOGGLES):
        states = _live_states(account.saved, catalog)
        back = hub and states == start
        rows = [{"value": a["id"], "label": a["name"], "notes": (LIVE_LABEL[states[a["id"]]],)} for a in catalog.apis]
        picked, page = _ask_paged(ctx, "Live-data APIs: pick one to change how Grounder uses it.", rows, keep=({"value": _ALL, "label": f"Set all to {LIVE_LABEL['auto']}"},),
                                  page=page, up=BACK_LABEL if back else "Done")
        if picked in (None, _RETURN):
            if back:
                return None if picked == _RETURN else UNCHANGED
            break
        if picked == _ALL:
            _save_live(account, catalog, {a["id"]: "auto" for a in catalog.apis})
        else:
            _step_api(ctx, account, catalog, picked)
    return _apis_words(_live_states(account.saved, catalog), catalog)


def _hub(ctx, catalog: Catalog, account: Account) -> tuple[bool, Optional[str]]:
    """``/search`` with no argument where there is no form: the hub. A pick closes the menu and is the reply: Let Grounder pick at once, a preset or a provider from its own screen, the
    live-data APIs from theirs once one is changed. A screen's "← Back" asks the hub again. Done, or a hub the user leaves, answers "Nothing changed.". ``(False, None)`` when nothing
    could ask here (the caller lists what there is); else ``(True, reply)``. Each change is written to the account when it is made."""
    for turn in range(MAX_TOGGLES):
        picked = ctx.ask_choice(PICK_QUESTION, _hub_rows(catalog, account.saved))
        if picked is None and not turn:
            return False, None
        if picked == DEV:
            return True, _pick_dev(account, catalog)
        if picked == ADDON:
            return True, _set_addon(account, catalog, direct_dev.state() != direct_dev.ADDON)
        if picked in (AUTO, "presets", "providers"):  # a search mode, then whether Grounder direct goes with it
            reply = _pick(account, catalog, AUTO) if picked == AUTO else _step_presets(ctx, account, catalog) if picked == "presets" else _step_providers(ctx, account, catalog)
            if reply is None or reply == UNCHANGED:
                if reply is None:
                    continue
                return True, reply
            before = direct_dev.addon_on()
            _ask_direct(ctx)
            reply = _with_direct(reply)
            if direct_dev.addon_on() != before:  # the mode was already set, but Grounder direct changed with it
                reply = reply.replace("✓ Search unchanged: ", "✓ Search switched: ", 1)
            return True, reply
        if picked == APIS:
            reply = _step_apis(ctx, account, catalog, hub=True)
        elif picked == SETTINGS:
            reply = _step_settings(ctx)
        else:  # Done, or a hub the user left
            break
        if reply is not None:
            return True, reply
    return True, UNCHANGED


def _listing(catalog: Catalog, usage: str, saved: dict[str, Any]) -> str:
    lines = [f"{OPTIMIZED_HEADER} (`/search optimized`, the default): {AUTO_LABEL}.",
             f"{PRESETS_HEADER} (`/search <preset>`):", *(f"  {p['id']} ({p['name']}): {p['description']}" for p in catalog.presets),
             f"{PROVIDERS_HEADER} (`/search <provider>` or `/search <provider>:<mode>`, the only search provider called), each provider with its modes:",
             *(f"  {p['id']} ({p['name']})" + (f": {', '.join(m['mode'] for m in p['modes'])}" if p["modes"] else "") for p in catalog.providers),
             "Grounder direct (on this Mac only): by itself (`/search direct`), it goes only to the address you give, as your Safari asks for it, with no search and no "
             "fallback; as an add-on (`/search addon on|off`), searches run in the mode above and every page and named site is read straight from its site. "
             "`/search-login <site>` saves a sign-in it then uses on every page of that site, `/search-logout <site>` forgets it; sign-ins are optional. "
             "`/search-direct-format` sets how a page comes back: full-page, highlights or summary."]
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
    """``/search [<id>|optimized|apis]``: the hub (or one form, on a Hermes that has one) for the mode and the live-data APIs, or an argument that skips it. ``auto`` is ``optimized``.
    A pick, from the hub or typed, is answered with what is set now (``_switched``).
    The choices are read from and written to the user's Grounder account (``Account``); a read or write that fails is the reply."""
    words = (raw_args or "").split()
    if len(words) >= 2 and words[0].lower() in DEV_WORDS and words[1].lower() in direct_dev.DEV_COMMANDS:  # saved sign-ins: on this Mac, no account needed
        return direct_dev.command(words[1:])
    catalog, problem = _catalog()
    if catalog is None:
        return problem
    try:
        return _run(ctx, catalog, Account.read(), raw_args)
    except api.ProviderNotSignedIn:
        return sign_in_first("how Grounder searches")
    except Failed as failure:
        return str(failure)


def _run(ctx, catalog: Catalog, account: Account, raw_args: str) -> Optional[str]:
    saved = account.saved
    usage = ("Use `/search <id>` to choose a Grounder preset or the one search provider to call (Grounder pinned; `/search <provider>:<mode>` for one of its modes), "
             "`/search optimized` to let Grounder pick the provider, `/search direct` for Grounder direct by itself, `/search addon on|off` for it as an add-on, or `/search apis` to set each live-data API to Every call, Never or Grounder decides.")
    arg = (raw_args or "").strip().lower()
    if not arg:
        if _has_form(ctx):
            return _form(ctx, catalog, account, _pages(catalog, saved))
        asked, reply = _hub(ctx, catalog, account)
        return reply if asked else _listing(catalog, usage, saved)
    if arg == SETTINGS:
        return _step_settings(ctx) or UNCHANGED
    if arg.startswith("resolver"):  # /search resolver grounder|system
        word = arg.partition(" ")[2].strip().replace("own", "system").replace("mine", "system")
        if word not in direct_dev.RESOLVERS:
            return "Use `/search resolver system` (your own resolver, the default) or `/search resolver grounder` (Grounder's resolver)."
        return _set_resolver(word)
    if arg == APIS:
        if _has_form(ctx):
            return _form(ctx, catalog, account, [{**_apis_page(catalog, saved), "optional": False, "hint": APIS_ALONE_HINT}])
        return _step_apis(ctx, account, catalog)
    if arg.startswith(f"{APIS} "):
        return _apis_argument(arg[len(APIS):].split(), catalog, account, usage)
    if arg in DEV_WORDS:
        return _pick_dev(account, catalog)
    words = arg.split()
    if len(words) == 2 and words[0] in DEV_WORDS and words[1] in ("with-search", "add-on", "addon", "off"):  # /search direct with-search | off
        return _set_addon(account, catalog, words[1] != "off")
    if words and words[0] in ADDON_WORDS and len(words) <= 2:  # /search addon [on|off]: no word toggles it
        wanted = {"on": True, "off": False}.get(words[1]) if len(words) == 2 else not direct_dev.addon_on()
        if wanted is None:
            return "Use `/search addon on` or `/search addon off`."
        return _set_addon(account, catalog, wanted)
    pick = AUTO if arg in (AUTO, OPTIMIZED) else _match(arg, catalog)[0]
    if pick is None:
        return f"{_match(arg, catalog)[1]}\n{_listing(catalog, usage, saved)}"
    if pick.startswith("api:"):  # a live-data API typed as a pick is Every call for it, and leaves the mode alone
        return _apis_words(_save_live(account, catalog, {pick.partition(":")[2]: "always"}), catalog)
    return _pick(account, catalog, pick)


def _apis_argument(tokens: list[str], catalog: Catalog, account: Account, usage: str) -> str:
    """``/search apis <id>=<setting> ...``: set live-data APIs without the page. A setting is ``every`` (Every call), ``never`` or ``auto`` (Grounder decides); the id or the
    name of an API, ``=`` or ``:``, and a setting. Every pair is checked before any is saved."""
    wanted: dict[str, str] = {}
    for token in tokens:
        name, _, setting = token.replace(":", "=", 1).partition("=")
        found = next((a for a in catalog.apis if name in (a["id"].lower(), a["name"].lower())), None)
        if found is None:
            return f"Grounder has no live-data API called {name}.\n{_listing(catalog, usage, account.saved)}"
        if setting not in LIVE_ANSWERS:
            return f"`{token}` is not a setting: use `{name}=every`, `{name}=never` or `{name}=auto` (Grounder decides)."
        wanted[found["id"]] = LIVE_ANSWERS[setting]
    return _apis_words(_save_live(account, catalog, wanted), catalog)


FORMAT_QUESTION = "How should Grounder direct return a page?"
FORMAT_NOTES = {"full-page": "the page's whole text", "highlights": "its passages that match the question", "summary": "answered in a few sentences"}


def _format_reply(fmt: str, was: Optional[str]) -> str:
    lines = [f"Pages: {FORMAT_NOTES[fmt]}", "A format you name in chat, or a call's own format, wins for that turn", "(saved on this machine: web.grounder.direct_format in config.yaml)"]
    if not direct_dev.reads_pages():
        lines.insert(0, "Grounder direct is off: `/search direct` or `/search addon on` to use it")
    return _block(f"Format {'unchanged' if was == fmt else 'switched'}: {fmt}", lines)


def search_format_command(ctx, raw_args: str) -> Optional[str]:
    """``/search-direct-format [full-page|highlights|summary]``: how a page Grounder direct reads comes back, and how the model answers from it. Saved on this machine only."""
    from . import direct_formats
    was = direct_formats.saved()
    arg = (raw_args or "").strip()
    current = was or direct_formats.FULL
    if arg:
        fmt = direct_formats.valid(arg)
        if fmt is None:
            return f"`{arg}` is not a format: use `/search-direct-format full-page`, `/search-direct-format highlights` or `/search-direct-format summary`."
    elif callable(getattr(ctx, "ask_choice", None)):
        rows = [{"value": f, "label": f, "tail": CURRENT if f == current else "", "notes": (FORMAT_NOTES[f],)} for f in direct_formats.FORMATS]
        fmt = ctx.ask_choice(FORMAT_QUESTION, _card(rows))
        if fmt not in direct_formats.FORMATS:
            return UNCHANGED
    else:
        return f"Format now: {current}. Use `/search-direct-format full-page`, `/search-direct-format highlights` or `/search-direct-format summary`."
    direct_formats.save(fmt)
    return _format_reply(fmt, was)


def register_commands(ctx) -> None:
    direct_dev.register_login(ctx)  # /search-login <site>: a saved sign-in for Grounder direct (the sign-in part of direct_dev)
    ctx.register_command(
        "search-direct-format", lambda raw_args: search_format_command(ctx, raw_args), args_hint="[full-page|highlights|summary]",
        description="Choose how Grounder direct returns a page: full-page, highlights or summary")
    ctx.register_command(
        "search", lambda raw_args: search_command(ctx, raw_args), args_hint="[direct|direct with-search|settings|optimized|preset|provider|provider:mode|apis]",
        description="Choose how Grounder searches: Grounder optimized, a Grounder preset, Grounder pinned (one provider and its mode) or Grounder direct (by itself, or as an add-on), and set each live-data API to "
                    "Every call, Never or Grounder decides (on Slack: /hermes search)")
