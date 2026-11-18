"""Public site vs the password-protected admin site.

The same dashboard runs twice on the Pi:

    smart-garden-dashboard  :8501  SMART_GARDEN_SITE=public  smartgarden.tech
    smart-garden-admin      :8502  SMART_GARDEN_SITE=admin   admin.smartgarden.tech

The public site never renders a control (settings, calibration, stage changes,
camera commands, downloads), so nothing on it can change the Pi or read its
files. Anything other than ``admin`` counts as public.

On the admin site ``AdminAuthMiddleware`` sits in front of every route,
Streamlit's websocket included: without a valid login cookie a browser gets the
login page and nothing else. The password's scrypt hash and the cookie signing
secret live in ``.streamlit/secrets.toml`` (git-ignored), written by
``.venv/bin/python scripts/set_admin_password.py``. With no hash there, nobody can log in.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import secrets
import time
import tomllib
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

SECRETS_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
COOKIE = "sg_admin"
SESSION_S = 30 * 24 * 3600
# PWA files are the same for everyone and the browser fetches some without cookies;
# the health check (for deploy-pi.sh) only answers "ok".
OPEN_PATHS = ("/login", "/manifest.webmanifest", "/sw.js", "/pwa/", "/_stcore/health")
MAX_FAILURES = 10           # wrong passwords per window, from anyone,
FAILURE_WINDOW_S = 15 * 60  # before login is refused until the window passes
_failures: list[float] = []


def is_admin_site() -> bool:
    return os.getenv("SMART_GARDEN_SITE", "").strip().lower() == "admin"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def load_secrets(path: Path | None = None) -> dict:
    try:
        with (path or SECRETS_PATH).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not data.get("admin_password_hash") or not data.get("admin_cookie_secret"):
        return {}
    return data


def _signing_key(conf: dict) -> bytes:
    # Includes the hash, so setting a new password logs every browser out.
    return hashlib.sha256(
        f"{conf['admin_cookie_secret']}|{conf['admin_password_hash']}".encode()).digest()


def make_token(conf: dict, now: float | None = None) -> str:
    expires = str(int((now or time.time()) + SESSION_S))
    mac = hmac.new(_signing_key(conf), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def token_ok(token: str | None, conf: dict, now: float | None = None) -> bool:
    if not token or not conf or "." not in token:
        return False
    expires, mac = token.split(".", 1)
    want = hmac.new(_signing_key(conf), expires.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def _cookie(scope) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    return val
    return None


def _is_open(path: str) -> bool:
    if ".." in path or "//" in path:
        return False
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)


class AdminAuthMiddleware:
    """Let a request through only with a valid login cookie (pure ASGI, so it
    also guards Streamlit's websocket)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] not in ("http", "websocket") or _is_open(scope.get("path", ""))
                or token_ok(_cookie(scope), load_secrets())):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        accept = dict(scope.get("headers") or []).get(b"accept", b"")
        wants_page = scope.get("method") == "GET" and b"text/html" in accept
        response = (RedirectResponse("/login", status_code=303) if wants_page
                    else Response("Log in at /login", status_code=401))
        await response(scope, receive, send)


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Garden admin</title>
<style>
  :root {{ color-scheme: light dark; --accent: #7c5cff; }}
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
          min-height: 100vh; margin: 0; padding: 0 16px; }}
  form {{ width: 100%; max-width: 320px; display: grid; gap: 12px; }}
  input, button {{ font: inherit; padding: 10px 12px; border-radius: 8px;
                   border: 1px solid #8886; }}
  button {{ background: var(--accent); color: white; border: 0; cursor: pointer; }}
  .msg {{ color: #d33; min-height: 1.2em; margin: 0; }}
</style></head>
<body><form method="post" action="/login">
  <h1>🌱 Smart Garden admin</h1>
  <input type="password" name="password" placeholder="Password" autofocus
         autocomplete="current-password" required>
  <button type="submit">Log in</button>
  <p class="msg">{message}</p>
</form></body></html>"""


def _login_page(message: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(_LOGIN_PAGE.format(message=html.escape(message)), status_code=status,
                        headers={"Cache-Control": "no-store"})


def _locked_for_s(now: float) -> int:
    while _failures and _failures[0] < now - FAILURE_WINDOW_S:
        _failures.pop(0)
    if len(_failures) < MAX_FAILURES:
        return 0
    return int(_failures[0] + FAILURE_WINDOW_S - now) + 1


async def login(request):
    conf = load_secrets()
    if not conf:
        return _login_page("No admin password is set. On the Pi run: "
                           ".venv/bin/python scripts/set_admin_password.py", 503)
    if request.method != "POST":
        if token_ok(request.cookies.get(COOKIE), conf):
            return RedirectResponse("/", status_code=303)
        return _login_page()
    locked = _locked_for_s(time.time())
    if locked:
        return _login_page(f"Too many wrong passwords. Try again in "
                           f"{max(1, locked // 60)} min.", 429)
    body = (await request.body()).decode("utf-8", "replace")
    password = (parse_qs(body).get("password") or [""])[0]
    if not check_password(password, conf["admin_password_hash"]):
        _failures.append(time.time())
        await asyncio.sleep(1)
        return _login_page("Wrong password.", 401)
    response = RedirectResponse("/", status_code=303)
    # Behind the Cloudflare tunnel the browser is on HTTPS; on the LAN it is plain HTTP.
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, make_token(conf), max_age=SESSION_S, httponly=True,
                        samesite="lax", secure=https)
    return response


async def logout(_request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


ROUTES = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout),
]
