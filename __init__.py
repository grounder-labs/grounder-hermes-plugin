# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder web search for Hermes Agent.

``register`` adds the ``grounder`` web search backend (``provider``). Sign-in is
Hermes's own: ``hermes auth add grounder:grounder`` signs in to the ``oauth:``
client ``plugin.yaml`` declares, and Hermes stores, refreshes and attaches the
grant, so this plugin keeps no credential of its own.
"""

from __future__ import annotations

from .provider import GrounderWebSearchProvider


def register(ctx) -> None:
    """Hermes's plugin entry point."""
    ctx.register_web_search_provider(GrounderWebSearchProvider())
