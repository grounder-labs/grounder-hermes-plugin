# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Load the plugin the way Hermes does (a package from its directory) against a stub Grounder."""

import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


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


class Stub:
    """Records every request; answers from per-path handlers."""

    def __init__(self):
        self.requests = []
        self.search_status = 200
        self.search_body = {"query": "q", "results": []}  # bytes are sent as they are, not as JSON
        self.rejected = {"stale-401"}  # access tokens /v1/search answers 401 for
        self.refresh_error = None
        self.refreshes = 0
        self.lock = threading.Lock()

    def handle(self, method, path, headers, body):
        self.requests.append({"method": method, "path": path,
                              "headers": headers, "body": body})
        if path == "/v1/search":
            if headers.get("Authorization", "").removeprefix("Bearer ") in self.rejected:
                return 401, {"error": {"code": "unauthorized", "message": "expired"}}
            return self.search_status, self.search_body
        if path == "/oauth/register":
            return 201, {"client_id": "client-1"}
        if path == "/oauth/token":
            form = urllib.parse.parse_qs(body)
            if form["grant_type"] == ["refresh_token"]:
                with self.lock:
                    self.refreshes += 1
                if self.refresh_error:
                    return 400, {"error": self.refresh_error}
                return 200, {"access_token": f"fresh-{self.refreshes}",
                             "refresh_token": f"refresh-{self.refreshes}",
                             "expires_in": 3600, "scope": "search"}
            return 200, {"access_token": "first", "refresh_token": "refresh-0",
                         "expires_in": 3600, "scope": "search"}
        return 404, {"error": "not_found"}


@pytest.fixture
def stub():
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode()
            status, payload = state.handle("POST", self.path, dict(self.headers), body)
            raw = isinstance(payload, bytes)
            data = payload if raw else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if raw else "application/json")
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
    module = _load_plugin()
    monkeypatch.setattr(module.auth, "ORIGIN", stub.origin)
    return module
