# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Grounder sign-in for Hermes: OAuth 2.0 authorization code with PKCE.

``login`` registers a public client named Hermes Agent, opens Grounder's consent
page and takes the code on a loopback callback (``127.0.0.1``). The grant is
for the ``search`` scope on ``/v1/search`` only: it searches, and reaches no MCP
tool and none of the account's settings, keys or billing.

Tokens are kept with mode 0600 at ``$HERMES_HOME/grounder/oauth.json``, written
atomically, and never printed or logged. Access tokens last one hour; refresh
tokens are single-use and rotate, so one thread or process refreshes at a time
under a file lock and the others use what it saved.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional

ORIGIN = "https://api.grounder.net"
SCOPE = "search"
USER_AGENT = "grounder-hermes/0.1.0"  # Grounder names these searches Hermes Agent; keep it on plugin.yaml's version
CLIENT_NAME = "Hermes Agent"  # the name Grounder's consent page asks the user to allow
REFRESH_MARGIN_S = 60  # refresh this long before the access token expires
LOGIN_TIMEOUT_S = 180  # how long login waits for the browser

_thread_lock = threading.Lock()


class AuthError(Exception):
    """A sign-in problem the user fixes with ``hermes grounder login``."""


def resource() -> str:
    """The one resource the grant is for (RFC 8707)."""
    return f"{ORIGIN}/v1/search"


def hermes_home() -> Path:
    """Hermes's home: its own answer when Hermes is loaded, else ``$HERMES_HOME`` or ``~/.hermes``."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def token_path() -> Path:
    """Where the grant is saved."""
    return hermes_home() / "grounder" / "oauth.json"


def _post(endpoint: str, payload: dict[str, Any], form: bool = False) -> dict[str, Any]:
    """POST to a Grounder OAuth endpoint; any failure is an ``AuthError`` that names no secret."""
    data = urllib.parse.urlencode(payload) if form else json.dumps(payload)
    content_type = ("application/x-www-form-urlencoded" if form
                    else "application/json")
    request = urllib.request.Request(
        f"{ORIGIN}{endpoint}", data.encode(),
        {"Content-Type": content_type, "Accept": "application/json",
         "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("error")
        except (ValueError, AttributeError):
            code = None
        if code == "invalid_grant":
            raise AuthError("Grounder sign-in has expired. "
                            "Run `hermes grounder login`.") from None
        detail = f" ({code})" if isinstance(code, str) else ""
        raise AuthError(f"Grounder {endpoint} returned HTTP "
                        f"{error.code}{detail}") from None


def _record(tokens: dict[str, Any], client_id: str,
            previous_refresh: Optional[str] = None) -> dict[str, Any]:
    """The record saved for a token response, refused unless it grants exactly the search scope."""
    if tokens.get("scope", SCOPE) != SCOPE:
        raise AuthError("Grounder did not grant the search scope")
    if not tokens.get("access_token"):
        raise AuthError("Grounder returned no access token")
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or previous_refresh,
        "expires_at": int(time.time()) + int(tokens.get("expires_in") or 3600),
        "scope": SCOPE,
        "client_id": client_id,
        "resource": resource(),
    }


def load_tokens() -> Optional[dict[str, Any]]:
    """The saved grant; None when there is none, an ``AuthError`` when it is damaged."""
    try:
        with open(token_path()) as handle:
            tokens = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise AuthError("Grounder token file is unreadable. "
                        "Run `hermes grounder login`.") from None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise AuthError("Grounder token file is incomplete. "
                        "Run `hermes grounder login`.")
    return tokens


def save_tokens(tokens: dict[str, Any]) -> None:
    """Write the grant with mode 0600 through a temporary file, so no reader sees half of it."""
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temporary)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(tokens, output, indent=2)
        output.write("\n")
    os.replace(temporary, path)


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Serialize refreshes across threads and, where flock exists, processes."""
    lock_path = token_path().with_name("oauth.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _thread_lock, open(lock_path, "a") as handle:
        try:
            import fcntl
        except ImportError:  # Windows: the thread lock still covers this process
            yield
            return
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def access_token() -> str:
    """A usable access token, refreshed first when it is about to expire."""
    tokens = load_tokens()
    if tokens is None:
        raise AuthError("Not signed in to Grounder. Run `hermes grounder login`.")
    if tokens.get("expires_at", 0) - REFRESH_MARGIN_S > time.time():
        return tokens["access_token"]
    return refresh(tokens["access_token"])


def refresh(stale: str) -> str:
    """Replace ``stale`` with a new access token, unless another caller already has."""
    with _locked():
        tokens = load_tokens()
        if tokens is None:
            raise AuthError("Not signed in to Grounder. "
                            "Run `hermes grounder login`.")
        if tokens["access_token"] != stale:
            return tokens["access_token"]
        if not tokens.get("refresh_token") or not tokens.get("client_id"):
            raise AuthError("Grounder sign-in has expired. "
                            "Run `hermes grounder login`.")
        fresh = _post("/oauth/token", {
            "grant_type": "refresh_token", "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"], "scope": SCOPE,
            "resource": resource(),
        }, form=True)
        record = _record(fresh, tokens["client_id"], tokens["refresh_token"])
        save_tokens(record)
        return record["access_token"]


def login(open_browser: bool = True, port: int = 0) -> Path:
    """Sign in through the browser and save a search-only grant."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(
        verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    callback: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            valid = values.get("state") == [state]
            if valid and len(values.get("code", [])) == 1:
                callback["code"] = values["code"][0]
            else:
                callback["error"] = "Sign-in was denied or the callback was invalid"
            self.send_response(200 if "code" in callback else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to your terminal. You may close this tab.")

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 5
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        client = _post("/oauth/register", {
            "redirect_uris": [redirect_uri], "client_name": CLIENT_NAME,
            "token_endpoint_auth_method": "none"})
        params = urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state,
            "resource": resource(), "scope": SCOPE,
        })
        url = f"{ORIGIN}/oauth/authorize?{params}"
        print(f"Sign in to Grounder and allow {CLIENT_NAME}:\n  {url}\n")
        print(f"Waiting for the browser on {redirect_uri} "
              f"({LOGIN_TIMEOUT_S}s, Ctrl+C to cancel)...")
        if open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in callback:
        raise AuthError(callback.get("error", "Sign-in timed out"))
    tokens = _post("/oauth/token", {
        "grant_type": "authorization_code", "client_id": client["client_id"],
        "code": callback["code"], "code_verifier": verifier,
        "redirect_uri": redirect_uri, "resource": resource(),
    }, form=True)
    with _locked():
        save_tokens(_record(tokens, client["client_id"]))
    return token_path()


def logout() -> bool:
    """Delete the saved grant; True when there was one."""
    with _locked():
        try:
            token_path().unlink()
        except FileNotFoundError:
            return False
    return True
