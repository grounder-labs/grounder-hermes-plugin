# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder web search for Hermes Agent.

``register`` adds two things: the ``grounder`` web search backend (``provider``)
and the ``hermes grounder login|status|logout`` command, which manages the
search-only sign-in (``auth``).
"""

from __future__ import annotations

import time

from . import auth
from .provider import KEY_ENV, GrounderWebSearchProvider


def _status() -> int:
    """Print which credential searches use and how long it lasts; 0 when one is usable."""
    from agent.web_search_provider import get_provider_env

    if get_provider_env(KEY_ENV):
        print(f"Using the Grounder search key in {KEY_ENV}.")
        return 0
    try:
        tokens = auth.load_tokens()
    except auth.AuthError as error:
        print(error)
        return 1
    if tokens is None:
        print("Not signed in. Run `hermes grounder login`.")
        return 1
    minutes = int((tokens.get("expires_at", 0) - time.time()) // 60)
    state = (f"access token valid for {minutes} min" if minutes > 0
             else "access token renews on the next search")
    print(f"Signed in to Grounder ({state}). Tokens: {auth.token_path()}")
    return 0


def _command(args) -> int:
    """Run ``hermes grounder <action>``; the return value is the exit code."""
    action = getattr(args, "grounder_command", None)
    if action == "login":
        try:
            path = auth.login(open_browser=not args.no_browser, port=args.port)
        except auth.AuthError as error:
            print(error)
            return 1
        print(f"Signed in. Tokens saved privately to {path}; no search was run.")
        print("Select Grounder with `hermes tools`, or set "
              "web.search_backend: grounder in config.yaml.")
        return 0
    if action == "logout":
        removed = auth.logout()
        print("Signed out; tokens deleted." if removed else "Not signed in.")
        return 0
    if action == "status":
        return _status()
    print("Usage: hermes grounder <login|status|logout>")
    return 1


def _setup(subparser) -> None:
    """Add the login, status and logout subcommands to ``hermes grounder``."""
    subs = subparser.add_subparsers(dest="grounder_command")
    login = subs.add_parser("login", help="Sign in to Grounder in the browser")
    login.add_argument("--no-browser", action="store_true",
                       help="Print the sign-in URL without opening a browser")
    login.add_argument("--port", type=int, default=0,
                       help="Fixed local callback port, for an SSH tunnel")
    subs.add_parser("status", help="Show the Grounder sign-in state")
    subs.add_parser("logout", help="Delete the saved Grounder sign-in")
    subparser.set_defaults(func=_command)


def register(ctx) -> None:
    """Hermes's plugin entry point."""
    ctx.register_web_search_provider(GrounderWebSearchProvider())
    ctx.register_cli_command(
        name="grounder", help="Sign in to Grounder web search",
        setup_fn=_setup, handler_fn=_command)
