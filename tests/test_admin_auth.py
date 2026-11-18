"""Admin site login: password hash, signed cookie, and the gate in front of every route."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import admin_auth  # noqa: E402
import set_admin_password  # noqa: E402

PASSWORD = "correct horse battery"


def test_password_hash_round_trip():
    stored = admin_auth.hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert admin_auth.check_password(PASSWORD, stored)
    assert not admin_auth.check_password("wrong", stored)
    assert not admin_auth.check_password(PASSWORD, "garbage")


def test_secrets_file_keeps_other_settings(tmp_path):
    path = tmp_path / "secrets.toml"
    path.write_text('admin_password_hash = "old"\n[mqtt]\nhost = "x"\n', encoding="utf-8")
    set_admin_password.write_secrets(PASSWORD, path)
    conf = admin_auth.load_secrets(path)
    assert admin_auth.check_password(PASSWORD, conf["admin_password_hash"])
    assert conf["mqtt"] == {"host": "x"}
    assert admin_auth.load_secrets(tmp_path / "missing.toml") == {}


def test_token_expires_and_dies_with_a_new_password(tmp_path):
    path = tmp_path / "secrets.toml"
    set_admin_password.write_secrets(PASSWORD, path)
    conf = admin_auth.load_secrets(path)
    token = admin_auth.make_token(conf, now=1000)
    assert admin_auth.token_ok(token, conf, now=1001)
    assert not admin_auth.token_ok(token, conf, now=1000 + admin_auth.SESSION_S + 1)
    assert not admin_auth.token_ok(token[:-1] + "0", conf, now=1001)
    assert not admin_auth.token_ok(None, conf)
    set_admin_password.write_secrets("another long password", path)
    assert not admin_auth.token_ok(token, admin_auth.load_secrets(path), now=1001)


def _request(app, method, path, headers=None, body=b"", cookies=None):
    """One request straight into the ASGI app: (status, headers, body)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": raw, "scheme": "http", "server": ("test", 80),
             "client": ("test", 1), "root_path": "", "http_version": "1.1"}
    sent, out = [], {"status": 0, "headers": [], "body": b""}

    async def receive():
        if sent:
            return {"type": "http.disconnect"}
        sent.append(1)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"], out["headers"] = message["status"], message["headers"]
        else:
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], {k.decode(): v.decode() for k, v in out["headers"]}, out["body"].decode()


def _form(password):
    return {"headers": {"content-type": "application/x-www-form-urlencoded"},
            "body": urlencode({"password": password}).encode()}


@pytest.fixture
def app(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    path = tmp_path / "secrets.toml"
    set_admin_password.write_secrets(PASSWORD, path)
    monkeypatch.setattr(admin_auth, "SECRETS_PATH", path)
    monkeypatch.setattr(admin_auth, "_failures", [])

    async def private(_request):
        return PlainTextResponse("garden settings")

    return Starlette(routes=admin_auth.ROUTES + [Route("/", private), Route("/settings", private),
                                                Route("/_stcore/health", private)],
                     middleware=[Middleware(admin_auth.AdminAuthMiddleware)])


def _cookie(headers):
    name, _, rest = headers["set-cookie"].partition("=")
    return {name: rest.split(";")[0]}


def test_gate_sends_pages_to_login_and_refuses_the_rest(app):
    status, headers, _ = _request(app, "GET", "/settings", {"accept": "text/html"})
    assert status == 303 and headers["location"] == "/login"
    assert _request(app, "GET", "/settings")[0] == 401
    assert "Password" in _request(app, "GET", "/login")[2]
    assert _request(app, "GET", "/_stcore/health")[0] == 200
    assert _request(app, "GET", "/pwa/../settings")[0] == 401


def test_websocket_without_cookie_is_closed(app):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    scope = {"type": "websocket", "path": "/_stcore/stream", "headers": []}
    asyncio.run(admin_auth.AdminAuthMiddleware(app)(scope, receive, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_login_sets_cookie_then_logout_clears_it(app):
    status, _, body = _request(app, "POST", "/login", **_form("nope"))
    assert status == 401 and "Wrong password" in body
    status, headers, _ = _request(app, "POST", "/login", **_form(PASSWORD))
    assert status == 303 and "httponly" in headers["set-cookie"].lower()
    cookies = _cookie(headers)
    assert _request(app, "GET", "/", cookies=cookies)[2] == "garden settings"
    status, headers, _ = _request(app, "GET", "/logout", cookies=cookies)
    assert status == 303 and 'sg_admin=""' in headers["set-cookie"]


def test_too_many_wrong_passwords_locks_login(app, monkeypatch):
    monkeypatch.setattr(admin_auth, "_failures", [1e12] * admin_auth.MAX_FAILURES)
    monkeypatch.setattr(admin_auth.time, "time", lambda: 1e12 + 1)
    assert _request(app, "POST", "/login", **_form(PASSWORD))[0] == 429


def test_site_defaults_to_public(monkeypatch):
    monkeypatch.delenv("SMART_GARDEN_SITE", raising=False)
    assert not admin_auth.is_admin_site()
    monkeypatch.setenv("SMART_GARDEN_SITE", "admin")
    assert admin_auth.is_admin_site()
