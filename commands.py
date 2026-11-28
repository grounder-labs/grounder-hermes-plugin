# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The Grounder search, page format and resolver choices in Hermes."""

from __future__ import annotations

from . import direct_dev, direct_formats

SEARCH_CHOICES = ("Grounder provider", "Grounder direct")
FORMAT_LABELS = {"summary": "Summary", "full-page": "Full page", "highlights": "Highlights"}
RESOLVER_LABELS = {"system": "Your own resolver", "grounder": "Grounder's resolver"}
UNCHANGED = "Nothing changed."


def _choices(options: dict[str, str], current: str) -> list[dict[str, str]]:
    return [{"value": key, "label": label + (" (current)" if key == current else "")}
            for key, label in options.items()]


def _set_search(value: str) -> str:
    direct_dev.set_state(value)
    label = SEARCH_CHOICES[value == direct_dev.ONLY]
    price = f"\n{direct_dev.SEARCH_PRICE}" if value == direct_dev.ONLY else ""
    return f"Search with: {label}{price}\n{direct_dev.saved_note()}"


def _set_resolver(value: str) -> str:
    direct_dev.set_resolver(value)
    direct_formats.forget_cached_searches()
    return f"Resolver: {RESOLVER_LABELS[value]}\n(saved on this machine)"


def search_command(ctx, raw_args: str) -> str:
    """Choose the search mode, page format or resolver."""
    arg = " ".join((raw_args or "").lower().split())
    current = direct_dev.state()
    if arg in ("direct", "grounder-direct"):
        return _set_search(direct_dev.ONLY)
    if arg in ("provider", "direct off", "grounder-provider"):
        return _set_search(direct_dev.OFF)
    if arg.startswith("format "):
        return search_format_command(ctx, arg.removeprefix("format "))
    if arg.startswith("resolver "):
        resolver = arg.removeprefix("resolver ")
        return _set_resolver(resolver) if resolver in RESOLVER_LABELS else "Use `/search resolver system` or `/search resolver grounder`."
    if arg and arg not in ("settings", "resolver"):
        return "Use /search provider, /search direct, /search format <format> or /search resolver <resolver>."
    ask = getattr(ctx, "ask_choice", None)
    if not callable(ask):
        return f"Search with: {SEARCH_CHOICES[current == direct_dev.ONLY]}. Use /search provider or /search direct."
    modes = dict(zip((direct_dev.OFF, direct_dev.ONLY), SEARCH_CHOICES))
    if not arg:
        choices = [*_choices(modes, current), {"value": "format", "label": "Page format"},
                   {"value": "resolver", "label": "Resolver"}, {"value": "done", "label": "Done"}]
        chosen = ask("Search with Grounder", choices)
        if chosen in modes:
            return _set_search(chosen)
        if chosen == "format":
            return search_format_command(ctx, "")
        if chosen != "resolver":
            return UNCHANGED
    chosen = ask("Which resolver should Grounder direct use?", _choices(RESOLVER_LABELS, direct_dev.resolver()))
    return _set_resolver(chosen) if chosen in RESOLVER_LABELS else UNCHANGED


def search_format_command(ctx, raw_args: str) -> str:
    """Save or display the format used for Grounder direct reads."""
    current = direct_formats.saved() or direct_formats.SUMMARY
    arg = (raw_args or "").strip()
    if arg:
        fmt = direct_formats.valid(arg)
        if fmt is None:
            return "Use /search-direct-format full-page, highlights or summary."
    else:
        ask = getattr(ctx, "ask_choice", None)
        if not callable(ask):
            return f"Page format: {current}."
        fmt = ask("How should Grounder direct return a page?", _choices(FORMAT_LABELS, current))
        if fmt not in FORMAT_LABELS:
            return UNCHANGED
    direct_formats.save(fmt)
    return f"Page format: {FORMAT_LABELS[fmt]}\n{direct_dev.saved_note('direct_format')}"


def register_commands(ctx) -> None:
    ctx.register_command(
        "search-direct-format", lambda raw_args: search_format_command(ctx, raw_args),
        args_hint="[full-page|highlights|summary]", description="Choose the Grounder direct page format")
    ctx.register_command(
        "search", lambda raw_args: search_command(ctx, raw_args),
        args_hint="[provider|direct|direct off|format|resolver]",
        description="Choose Grounder provider, Grounder direct, page format or resolver. On Slack: /hermes search.")
