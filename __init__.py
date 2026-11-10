# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder web search for Hermes Agent.

``register`` adds the ``grounder`` web search backend (``provider``) and, the first
time it runs in a profile, selects it (``_select_once``). Sign-in is Hermes's own:
``hermes auth add grounder:grounder`` signs in to the ``oauth:`` client
``plugin.yaml`` declares, and Hermes stores, refreshes and attaches the grant, so
this plugin keeps no credential of its own. Two hooks ask the model to cite a read
Grounder served and end an answer that searched with the line Grounder's card shows. Two tools
carry what Hermes's ``web_search`` cannot: every option of Grounder's MCP search, and its feature list.
"""

from __future__ import annotations

import logging

from agent.secret_scope import get_secret
from hermes_cli.config import is_managed, save_config, save_env_value

from .provider import LIST_PROVIDERS_SCHEMA, SEARCH_SCHEMA, TOOLSET, CLIENT_ENV, CLIENT_ID, KEY_ENV, GrounderWebSearchProvider, note_turn

logger = logging.getLogger(__name__)

SELECTED_MARK = "backends_selected"  # in the plugin's own per-profile state: this profile has been set up


def _select_once(ctx) -> None:
    """Make Grounder this profile's web_search and web_extract backend, once.

    Hermes runs no plugin code at install, so this happens at the plugin's first
    load. The mark is written last, and only here: a later choice in ``hermes
    tools`` stands, and a failed write is tried again at the next load. With no
    search key the public sign-in client is filled in too, because Hermes turns
    the web tools off when the selected backend is not ready.
    """
    if ctx.state.get(SELECTED_MARK) or is_managed():
        return
    if not str(get_secret(KEY_ENV) or "").strip() and not str(get_secret(CLIENT_ENV) or "").strip():
        save_env_value(CLIENT_ENV, CLIENT_ID)
    save_config({"web": {"search_backend": "grounder", "extract_backend": "grounder"}}, merge_existing=True)
    ctx.state.set(SELECTED_MARK, True)
    logger.info("Grounder selected as the web_search and web_extract backend")


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
    ctx.register_hook("pre_tool_call", note_turn)
    try:
        _select_once(ctx)
    except Exception as error:  # a config that cannot be written must not take the plugin down with it
        logger.warning("Could not select Grounder as the web backend: %s", error)
