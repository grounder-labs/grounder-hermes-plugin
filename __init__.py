# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder web search for Hermes Agent.

``register`` adds the ``grounder`` web search backend (``provider``) and, the first
time it runs in a profile, selects it where the user has chosen no backend (``_select_once``). Sign-in is Hermes's own:
``hermes auth add grounder:grounder`` signs in to the ``oauth:`` client
``plugin.yaml`` declares, and Hermes stores, refreshes and attaches the grant, so
this plugin keeps no credential of its own. Two hooks ask the model to cite a read
Grounder served and end an answer that searched with the line Grounder's card shows. Two tools
carry what Hermes's ``web_search`` cannot: every option of Grounder's MCP search, and its feature list.
Grounder has three modes, and ``/search``, the plugin's one command, chooses which a signed-in search uses (``commands``), saving the choice on the user's Grounder account, where grounder.net's Routing page shows the same one: Grounder optimized (Grounder picks the provider for the
question; the default), a Grounder preset (Fast, Cheap, Live, Deep, Code or Balanced) or Grounder direct (the user picks the provider, and optionally its mode). Every mode returns
the provider's own results: this plugin cuts and reranks nothing of them. ``/search`` is registered only while Grounder is the profile's web search backend (``_grounder_is_backend``, read at load: a switch in ``hermes tools`` takes effect at the
next start), and a Hermes that bundles its own Grounder provider has the command, so there ``register`` adds none (``_commands_taken``).
"""

from __future__ import annotations

import logging
from importlib.util import find_spec

from agent.secret_scope import get_secret
from hermes_cli.config import is_managed, load_config, save_config, save_env_value

from .commands import register_commands
from .provider import LIST_PROVIDERS_SCHEMA, SEARCH_SCHEMA, TOOLSET, CLIENT_ENV, CLIENT_ID, KEY_ENV, GrounderWebSearchProvider, note_message, note_model, note_turn, sharing_usage

logger = logging.getLogger(__name__)

SELECTED_MARK = "backends_selected"  # in the plugin's own per-profile state: this profile has been set up
BUNDLED = "plugins.web.grounder"  # Hermes's own Grounder web provider, in the Hermes releases that ship one; it registers the same command
COMMANDS = ("search",)


def _bundled() -> bool:
    """Whether this Hermes ships its own ``grounder`` web provider.

    Asked of the import system, not of the commands registry: a Hermes registers bundled backends before installed plugins, but a registry shows only what
    has loaded so far, and under ``plugins.isolation: host`` this code runs in a process whose registry holds none of the bundled ones. Whether the package
    exists depends on neither.
    """
    try:
        return find_spec(BUNDLED) is not None
    except ValueError:  # already imported, with no spec set
        return True
    except ImportError:  # no plugins package, or a Hermes from before the bundled web providers
        return False


def _commands_taken(ctx) -> bool:
    """Whether another plugin already holds ``/search``. This plugin's own entry, from an earlier load, does not count."""
    try:
        held = ctx._manager._plugin_commands
        return any(name in held and held[name].get("plugin_key") != getattr(ctx, "plugin_id", None) for name in COMMANDS)
    except Exception:  # a Hermes that keeps its commands elsewhere: the registration itself refuses a name a built-in command holds
        return False


def _grounder_is_backend() -> bool:
    """Whether this profile's web search backend is Grounder, as ``config.yaml`` says now (``web.search_backend``, else the older ``web.backend``). Read at load, so a switch made in
    ``hermes tools`` takes effect at the next start. A config that cannot be read, or names no backend, is not Grounder."""
    try:
        web = load_config().get("web")
    except Exception as error:
        logger.warning("Could not read the web backend: %s", error)
        return False
    if not isinstance(web, dict):
        return False
    return str(web.get("search_backend") or web.get("backend") or "").strip().lower() == "grounder"


def _add_commands(ctx) -> None:
    """Register ``/search``, the plugin's one command, only while Grounder is this profile's web search backend (``_grounder_is_backend``): with another backend selected the plugin
    registers no command at all. Not when this Hermes cannot ask a choice or register a command, or already has the command from Grounder's bundled provider or another plugin.
    Search, the tools and the web provider work without it."""
    if not (callable(getattr(ctx, "register_command", None)) and callable(getattr(ctx, "ask_choice", None))):
        return
    if _bundled() or _commands_taken(ctx):
        logger.info("Hermes already has /search for Grounder: this plugin adds none")
        return
    if not _grounder_is_backend():
        logger.info("Grounder is not this profile's web search backend: /search is not registered")
        return
    register_commands(ctx)


def _unset_backends() -> list[str]:
    """The backends of ``web`` the user has not chosen: ``search_backend`` and ``extract_backend``, each unset when neither it nor the older shared ``backend`` names one
    (Hermes reads a capability's own key, then ``backend``). A config that cannot be read chooses nothing for the plugin to fill in."""
    try:
        web = load_config().get("web")
    except Exception as error:
        logger.warning("Could not read the web backends: %s", error)
        return []
    web = web if isinstance(web, dict) else {}
    shared = str(web.get("backend") or "").strip()
    return [key for key in ("search_backend", "extract_backend") if not shared and not str(web.get(key) or "").strip()]


def _select_once(ctx) -> None:
    """Make Grounder this profile's web_search and web_extract backend, once, where the user has chosen none.

    Hermes runs no plugin code at install, so this happens at the plugin's first
    load. A backend the user already set is left alone, each of the two on its own
    (``_unset_backends``): someone who installs this for ``grounder_search`` keeps
    the backend they chose, and one who set only a search backend gets Grounder to
    read pages. The mark is written last, and only here: a later choice in ``hermes
    tools`` stands, and a failed write is tried again at the next load. With no
    search key the public sign-in client is filled in too, so that ``hermes auth
    add`` needs no ``hermes config set`` before it. A search works without either:
    with no key and no sign-in it is one of Grounder's free searches.
    """
    if ctx.state.get(SELECTED_MARK) or is_managed():
        return
    if not str(get_secret(KEY_ENV) or "").strip() and not str(get_secret(CLIENT_ENV) or "").strip():
        save_env_value(CLIENT_ENV, CLIENT_ID)
    unset = _unset_backends()
    if unset:
        save_config({"web": {key: "grounder" for key in unset}}, merge_existing=True)
    ctx.state.set(SELECTED_MARK, True)
    if unset:
        logger.info("Grounder selected as the backend for %s", ", ".join(unset))
    else:
        logger.info("A web backend was already chosen: Grounder selected none")


def register(ctx) -> None:
    """Hermes's plugin entry point."""
    provider = GrounderWebSearchProvider()
    ctx.register_web_search_provider(provider)
    ctx.register_hook("transform_tool_result", provider.after_tool)
    ctx.register_hook("transform_llm_output", provider.after_answer)
    try:
        for schema, handler in ((SEARCH_SCHEMA, provider.search_tool), (LIST_PROVIDERS_SCHEMA, provider.list_providers_tool)):
            ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler)
    except Exception as error:  # a Hermes that refuses a tool must not take web search down with it
        logger.warning("Could not add Grounder's search tools: %s", error)
    try:  # first, so the load that makes Grounder the backend is the one that registers /search
        _select_once(ctx)
    except Exception as error:  # a config that cannot be written must not take the plugin down with it
        logger.warning("Could not select Grounder as the web backend: %s", error)
    try:
        _add_commands(ctx)
    except Exception as error:  # a Hermes that refuses a command must not take web search down with it
        logger.warning("Could not add Grounder's commands: %s", error)
    ctx.register_hook("pre_tool_call", note_turn)
    if sharing_usage():  # the model behind each answer is learned only while share_usage_context is on
        ctx.register_hook("post_api_request", note_model)
    ctx.register_hook("pre_llm_call", note_message)
