# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Load the plugin the way Hermes does (a package from its directory) against a stub Grounder.

In a Hermes Agent development environment the plugin imports Hermes's own interfaces. Anywhere else, stand-ins with
the same names and signatures take their place, so the tests run in any Python with pytest, PyYAML and httpx. Either
way, ``credentialed_provider_request`` is replaced by ``FakeHermes``, which plays Hermes's part: it refuses an
origin the manifest's ``oauth:`` hosts do not name, raises ``ProviderNotSignedIn`` with no grant, and attaches the
token itself. No test reaches the live API: a request to anywhere but the stub fails the test that made it.
"""

import abc
import importlib.util
import json
import os
import re
import sys
import threading
import types
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

try:
    import yaml
except ImportError:  # Hermes's environment has no PyYAML; it reads manifests with its own module
    import hermes_yaml as yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _install_hermes_stand_ins():
    """Hermes's interfaces the plugin imports, when Hermes is not installed here."""
    try:
        import agent.web_search_provider  # noqa: F401
        import hermes_cli.plugin_provider_requests  # noqa: F401
        return
    except ImportError:
        pass

    agent = types.ModuleType("agent")
    web = types.ModuleType("agent.web_search_provider")

    class WebSearchProvider(abc.ABC):
        @property
        @abc.abstractmethod
        def name(self) -> str: ...

        @abc.abstractmethod
        def is_available(self) -> bool: ...

    web.WebSearchProvider = WebSearchProvider
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: os.environ.get(name, default)

    hermes_cli = types.ModuleType("hermes_cli")
    constants = types.ModuleType("hermes_cli.auth_constants")

    class AuthError(RuntimeError):
        def __init__(self, message, *, provider="", code=None, **_kwargs):
            super().__init__(message)
            self.provider = provider
            self.code = code

    constants.AuthError = AuthError
    requests = types.ModuleType("hermes_cli.plugin_provider_requests")

    class ProviderNotSignedIn(RuntimeError):
        pass

    def credentialed_provider_request(*_args, **_kwargs):
        raise AssertionError("the tests replace credentialed_provider_request with FakeHermes")

    requests.ProviderNotSignedIn = ProviderNotSignedIn
    requests.credentialed_provider_request = credentialed_provider_request
    config = types.ModuleType("hermes_cli.config")

    def home():
        return Path(os.environ["HERMES_HOME"])

    def save_config(partial, *, merge_existing=False):
        path = home() / "config.yaml"
        saved = (yaml.safe_load(path.read_text()) or {}) if merge_existing and path.exists() else {}
        for section, values in partial.items():
            saved.setdefault(section, {}).update(values)
        path.write_text(yaml.safe_dump(saved))

    def save_env_value(name, value):
        with (home() / ".env").open("a") as env:
            env.write(f"{name}={value}\n")
        os.environ[name] = value

    def load_config():
        path = home() / "config.yaml"
        return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}

    config.is_managed = lambda: False
    config.load_config = load_config
    config.save_config = save_config
    config.save_env_value = save_env_value
    for module in (agent, web, scope, hermes_cli, constants, requests, config):
        sys.modules[module.__name__] = module


_install_hermes_stand_ins()


def _load_plugin():
    for name in [n for n in sys.modules if n.split(".")[0] == "grounder_hermes_plugin"]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "grounder_hermes_plugin", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest():
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())


SETTINGS = json.loads((Path(__file__).parent / "api_shapes.json").read_text())["settings"]
SEARCH_KEYS = ("mode", "profile", "requested_provider", "provider_mode", "fallback")
SETTINGS_SHAPE = ('Body must be {"search"?: {"mode": "optimized" | "preset" | "direct", …}, "specialized"?: {"<live-data API>": {"use": "auto" | "always" | "off"}}}: '
                  "a search credential sets these two and nothing else on the account.")


class Stub:
    """Records every request; answers /v1/search, and GET and PUT /v1/search/settings as the server does (workers/router/src/routes/search-settings.ts)."""

    def __init__(self):
        self.requests = []
        self.search_status = 200
        self.search_headers = {}  # extra headers on the /v1/search answer, e.g. a redirect's Location
        self.search_body = {"query": "q", "results": []}  # bytes are sent as they are, not as JSON
        self.search_script = []  # (status, body) answers to the next searches, in order, before search_status and search_body apply
        self.features_body = {"features": []}
        self.providers_body = {"providers": [], "live_data_apis": []}
        self.list_status = 200  # what /v1/providers and /v1/search-profiles answer
        self.profiles_body = {"profiles": []}
        self.rejected = {"revoked-token", "revoked-key"}  # bearers /v1/search answers 401 for
        # The account's saved search setting, the one grounder.net's Routing page shows too: a test that changes these plays a change made on the site.
        self.saved_search = dict(SETTINGS["answer"]["search"])
        self.saved_live = {}  # {live-data API id: "always" | "off"}; one not here is Grounder decides
        self.off_providers = set()  # providers turned off on the account: saving one as the search provider is a 400
        self.settings_script = []  # (status, body) answers to the next reads and writes of the setting, in order, before the stub answers as the server does

    def settings_answer(self):
        live = {api["id"]: {"use": self.saved_live.get(api["id"], "auto")} for api in self.providers_body.get("live_data_apis", [])}
        live.update({ident: {"use": use} for ident, use in self.saved_live.items()})
        return {**SETTINGS["answer"], "search": dict(self.saved_search), "specialized": live}

    def apply_settings(self, change):
        """The server's PUT: the 400 message when it refuses ``change`` (nothing is saved then), else None once saved."""
        if not isinstance(change, dict) or not change:
            return SETTINGS_SHAPE
        other = next((key for key in change if key not in ("search", "specialized")), None)
        if other is not None:
            return f"{other} is not part of the saved search setting. {SETTINGS_SHAPE}"
        search, live = dict(self.saved_search), dict(self.saved_live)
        if "search" in change:
            given = change["search"]
            if not isinstance(given, dict):
                return 'search must be {"mode": "optimized" | "preset" | "direct", …}'
            unknown = next((key for key in given if key not in SEARCH_KEYS), None)
            if unknown is not None:
                return f"Unknown setting: search.{unknown}"
            mode = given.get("mode")
            if mode not in ("optimized", "preset", "direct"):
                return "search.mode must be one of optimized, preset, direct"
            allowed = {"optimized": (), "preset": ("profile",), "direct": ("requested_provider", "provider_mode", "fallback")}[mode]
            stray = next((key for key in SEARCH_KEYS[1:] if key not in allowed and given.get(key) is not None), None)
            if stray is not None:
                return f"search.{stray} does not go with mode {mode}."
            search = dict(SETTINGS["answer"]["search"])
            if mode == "preset":
                presets = [row["id"] for row in self.profiles_body.get("profiles", [])]
                if given.get("profile") not in presets:
                    return f"search.profile must be one of {', '.join(presets)}"
                search.update(mode="preset", profile=given["profile"])
            if mode == "direct":
                owners = {row["provider"]: row for row in self.providers_body.get("providers", [])}
                provider = given.get("requested_provider")
                if provider not in owners:
                    return "search.requested_provider must be one of Grounder's providers (GET /v1/providers lists them)."
                modes = [o["mode"] for o in owners[provider].get("offerings", []) if o.get("mode")]
                picked = given.get("provider_mode")
                if picked not in (None, "default") and picked not in modes:
                    return f"search.provider_mode: no mode {picked}; {owners[provider].get('display_name', provider)}'s modes are {', '.join(modes) or 'none'}"
                if given.get("fallback") not in (None, True, False):
                    return "search.fallback must be true or false"
                if provider in self.off_providers:
                    return f"{owners[provider].get('display_name', provider)} is your search provider and is turned off: turn it on, or choose another provider for your searches first."
                search.update(mode="direct", requested_provider=provider, provider_mode=None if picked in (None, "default") else picked, fallback=given.get("fallback") is True)
        known = {api["id"] for api in self.providers_body.get("live_data_apis", [])} | set(self.saved_live)
        for ident, entry in (change.get("specialized") or {}).items():
            if ident not in known:
                return f"No such specialized API: {ident}"
            use = entry.get("use") if isinstance(entry, dict) else None
            if use not in ("auto", "always", "off"):
                return f"specialized.{ident}.use must be one of auto, always, off"
            live.pop(ident, None) if use == "auto" else live.__setitem__(ident, use)
        self.saved_search, self.saved_live = search, live
        return None

    def settings(self, method, body):
        if self.settings_script:
            return self.settings_script.pop(0)
        if method == "GET":
            return 200, self.settings_answer()
        refusal = self.apply_settings(json.loads(body or "null"))
        if refusal is not None:
            return 400, {"error": {"code": "invalid_request", "message": refusal}}
        return 200, self.settings_answer()

    def handle(self, method, path, headers, body):
        self.requests.append({"method": method, "path": path, "headers": headers, "body": body})
        if path.partition("?")[0] == "/v1/search/settings":
            return self.settings(method, body)
        if path == "/v1/search":
            if headers.get("Authorization", "").removeprefix("Bearer ") in self.rejected:
                return 401, {"error": {"code": "unauthorized", "message": "expired"}}
            if self.search_script:
                return self.search_script.pop(0)
            return self.search_status, self.search_body
        if path == "/v1/features":
            return 200, self.features_body
        if path in ("/v1/providers", "/v1/search-profiles"):
            if self.list_status != 200:
                return self.list_status, {"error": {"code": "unavailable", "message": "later"}}
            return 200, self.providers_body if path == "/v1/providers" else self.profiles_body
        return 404, {"error": "not_found"}


class FakeHermes:
    """``credentialed_provider_request`` as Hermes runs it for a plugin's own ``oauth:`` entry, against the stub."""

    def __init__(self, plugin, stub):
        self.plugin = plugin
        self.stub = stub
        self.token = None  # no grant until a test signs in
        self.raises = None  # an exception Hermes raises in place of a response
        self.calls = []
        entry = manifest()["oauth"][0]
        self.oauth_id = entry["id"]
        self.hosts = set(entry["hosts"])

    def __call__(self, provider, method, url, *, json=None, headers=None, timeout=30.0):
        self.calls.append({"provider": provider, "method": method, "url": url, "json": json,
                           "headers": dict(headers or {}), "timeout": timeout})
        if provider != self.oauth_id:
            raise ValueError(f"No signed-in requests for provider {provider!r}")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts or parsed.port not in (None, 443):
            raise PermissionError(f"Refusing to send the {provider} sign-in to {url!r}")
        if self.raises is not None:
            raise self.raises
        if self.token is None:
            raise self.plugin.provider.ProviderNotSignedIn("not signed in")
        sent = {name: value for name, value in (headers or {}).items() if name.lower() != "authorization"}
        response = httpx.request(method, f"{self.stub.origin}{parsed.path}", json=json, timeout=timeout,
                                 headers={**sent, "Authorization": f"Bearer {self.token}"})
        return types.SimpleNamespace(status=response.status_code, headers=dict(response.headers), body=response.content)


@pytest.fixture
def stub():
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            status, payload = state.handle("GET", self.path, dict(self.headers), "")
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            self.do_POST("PUT")

        def do_POST(self, method="POST"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode()
            status, payload = state.handle(method, self.path, dict(self.headers), body)
            raw = isinstance(payload, bytes)
            data = payload if raw else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if raw else "application/json")
            for name, value in state.search_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.origin = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def plugin(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GROUNDER_API_KEY", raising=False)
    monkeypatch.delenv("GROUNDER_OAUTH_CLIENT_ID", raising=False)
    # urlopen follows redirects, and with them a search key: the plugin sends every request through its own opener (``_open``), which follows none.
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("the plugin called urlopen, which follows redirects; use provider._open"))
    # Hermes's address check asks DNS; a test must not. This stand-in answers from the name alone: a host named ``resolves-private`` is one that resolves to a private address.
    try:
        import tools  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    safety = types.ModuleType("tools.url_safety")
    safety.is_safe_url = lambda url: "resolves-private" not in url
    monkeypatch.setitem(sys.modules, "tools.url_safety", safety)
    module = _load_plugin()
    # ``register`` asks the import system whether this Hermes bundles its own Grounder provider (``_bundled``, ``find_spec("plugins.web.grounder")``), and the answer is
    # the machine's: a Hermes checkout on ``PYTHONPATH``, or run from in, holds one, and then every test that registers the commands finds none. 0.6.1's run
    # of this suite failed 6 such tests once in four, by where it was started from. Every test starts with no bundled provider; one that needs the other answer says so.
    monkeypatch.setattr(module, "find_spec", lambda name: None)
    real = module.provider._open

    def to_stub(request, timeout):
        """The plugin's own requests (a key's search, a free search, a public list) reach the stub where they would reach Grounder; any other address fails the test."""
        url = request.full_url
        if url.startswith("https://api.grounder.net/"):
            request = urllib.request.Request(stub.origin + url[len("https://api.grounder.net"):], data=request.data,
                                             headers=dict(request.header_items()), method=request.get_method())
        else:
            assert url.startswith(stub.origin) or url.startswith("http://127.0.0.1:"), f"a test reached {url}, not the stub"
        return real(request, timeout)

    monkeypatch.setattr(module.provider, "_open", to_stub)
    return module


@pytest.fixture
def hermes(plugin, stub, monkeypatch):
    fake = FakeHermes(plugin, stub)
    monkeypatch.setattr(plugin.provider, "credentialed_provider_request", fake)
    return fake


@pytest.fixture
def keyed(plugin, stub, monkeypatch):
    """A search key in the environment, its searches sent to the stub (Hermes plays no part in them)."""
    monkeypatch.setenv("GROUNDER_API_KEY", "user-key")
    monkeypatch.setattr(plugin.provider, "SEARCH_URL", f"{stub.origin}/v1/search")
    return "user-key"


@pytest.fixture
def free(hermes):
    """No key and no grant: Hermes raises ``ProviderNotSignedIn``, and a free search goes to the stub with no credential."""
    return hermes


@pytest.fixture
def in_chat(monkeypatch):
    """The session the tests' hooks name (``session_id="s"``) is the one the tool calls run in, as in Hermes."""
    monkeypatch.setenv("HERMES_SESSION_ID", "s")
