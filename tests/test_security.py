#!/usr/bin/env python
"""Checks for server/security.py: the local API token and the CORS policy.

The backend can run shell commands and edit files, and it listens on a port any
process or web page on the machine can reach. These checks pin down the rules:

  * with JARVIS_API_TOKEN set, every route except the public ones needs the
    token, in the X-Jarvis-Token header (or ?token= for GET/EventSource);
  * a wrong token is rejected;
  * preflight requests are answered and only the app origin is allowed;
  * without the env var the API stays open (dev convenience) and says so.

Run:
    python tests/test_security.py

No pytest, no network.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import security  # noqa: E402

FAILED = []
TOKEN = "test-token-0123456789abcdef"


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class env:
    """Set/clear JARVIS_API_TOKEN for one block."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(security.TOKEN_ENV)
        if self.value is None:
            os.environ.pop(security.TOKEN_ENV, None)
        else:
            os.environ[security.TOKEN_ENV] = self.value

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop(security.TOKEN_ENV, None)
        else:
            os.environ[security.TOKEN_ENV] = self.saved


def make_app():
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "jarvis-backend"}

    @app.get("/conversations")
    def conversations():
        return {"items": []}

    @app.post("/agent/chat")
    def chat():
        return {"ok": True}

    security.install(app)
    return app


def main():
    with env(TOKEN):
        client = TestClient(make_app())
        h = {security.TOKEN_HEADER: TOKEN}

        check("health is public", client.get("/health").status_code == 200)
        check("docs are public", client.get("/openapi.json").status_code in (200, 404))

        r = client.get("/conversations")
        check("a protected route rejects a missing token", r.status_code == 401, r.status_code)
        check("the rejection says why", "token" in r.text.lower(), r.text[:120])

        r = client.get("/conversations", headers={security.TOKEN_HEADER: "wrong"})
        check("a wrong token is rejected", r.status_code == 401, r.status_code)

        r = client.get("/conversations", headers=h)
        check("the right token is accepted", r.status_code == 200, r.status_code)

        r = client.post("/agent/chat", headers=h)
        check("POST works with the token", r.status_code == 200, r.status_code)

        r = client.post(f"/agent/chat?token={TOKEN}")
        check("the token is not accepted in a POST query", r.status_code == 401, r.status_code)

        r = client.get(f"/conversations?token={TOKEN}")
        check("GET may pass the token in the query (EventSource)",
              r.status_code == 200, r.status_code)

        # CORS: the app origin is allowed, a random website is not.
        app_origin = "http://tauri.localhost"
        r = client.get("/conversations", headers={**h, "Origin": app_origin})
        check("the app origin is allowed",
              r.headers.get("access-control-allow-origin") == app_origin,
              r.headers.get("access-control-allow-origin"))

        r = client.get("/conversations", headers={**h, "Origin": "https://evil.example"})
        check("an unknown origin gets no CORS grant",
              "access-control-allow-origin" not in r.headers,
              r.headers.get("access-control-allow-origin"))

        r = client.options(
            "/agent/chat",
            headers={
                "Origin": app_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": security.TOKEN_HEADER,
            },
        )
        check("preflight succeeds without a token", r.status_code in (200, 204), r.status_code)
        check("preflight allows the token header",
              security.TOKEN_HEADER in (r.headers.get("access-control-allow-headers") or "").lower(),
              r.headers.get("access-control-allow-headers"))

        r = client.options(
            "/agent/chat",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        check("preflight from an unknown origin is not granted",
              "access-control-allow-origin" not in r.headers,
              r.headers.get("access-control-allow-origin"))

    with env(None):
        client = TestClient(make_app())
        check("without the env var the API stays open for local development",
              client.get("/conversations").status_code == 200)

    # The shell must pass the token and the UI must be able to read it.
    main_rs = open(os.path.join(ROOT, "src-tauri", "src", "main.rs"), encoding="utf-8").read()
    check("Rust passes JARVIS_API_TOKEN to the backend", '"JARVIS_API_TOKEN"' in main_rs)
    check("Rust exposes the token to the UI", "fn api_token" in main_rs or "api_token" in main_rs)

    api_ts = open(os.path.join(ROOT, "src", "app", "api.ts"), encoding="utf-8").read()
    check("the UI sends the token header", "x-jarvis-token" in api_ts.lower())
    check("the UI fetches the token from Rust", "api_token" in api_ts)

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("All security checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
