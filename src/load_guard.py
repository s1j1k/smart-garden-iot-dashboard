"""Keep the public site usable under a crowd, and the Pi free for the admin site.

Every open dashboard tab is a Streamlit session with its own websocket, which
reruns its live sections on a timer. On a Raspberry Pi 4 a crowd of visitors
can use up the CPU and memory, so the public site (not the admin site) admits a
new visitor only while:

* fewer than MAX_SESSIONS tabs are connected, and
* the Pi has at least MIN_FREE_MB of memory available (/proc/meminfo).

Otherwise a new page load gets static_pages/busy.html (HTTP 503): a green "high
load" page that tries again by itself every 20 s. Tabs already open keep
working. The same page is the nginx fallback when the public site is down
(deploy/nginx/smart-garden-public.conf).

    SMART_GARDEN_MAX_SESSIONS   default 60
    SMART_GARDEN_MIN_FREE_MB    default 350
"""

from __future__ import annotations

import os
from pathlib import Path

BUSY_PAGE = Path(__file__).resolve().parent / "static_pages" / "busy.html"
STREAM_PATH = "/_stcore/stream"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def free_mb(meminfo: Path = Path("/proc/meminfo")) -> float | None:
    """MemAvailable in MB, or None where /proc/meminfo does not exist (Windows)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def busy_reason(sessions: int, available_mb: float | None, max_sessions: int,
                min_free_mb: float) -> str | None:
    """Why a new visitor should wait, or None to let them in."""
    if sessions >= max_sessions:
        return f"{sessions} visitors connected"
    if available_mb is not None and available_mb < min_free_mb:
        return f"{available_mb:.0f} MB free"
    return None


class LoadGuardMiddleware:
    """Pure ASGI: counts open Streamlit websockets and answers new page loads with
    the busy page when the Pi is full. Only the page itself is refused; scripts,
    styles and the websockets of tabs already open pass through."""

    def __init__(self, app, max_sessions: int | None = None, min_free_mb: float | None = None,
                 meminfo: Path = Path("/proc/meminfo")):
        self.app = app
        self.sessions = 0
        self.max_sessions = max_sessions or _int_env("SMART_GARDEN_MAX_SESSIONS", 60)
        self.min_free_mb = (min_free_mb if min_free_mb is not None
                            else _int_env("SMART_GARDEN_MIN_FREE_MB", 350))
        self.meminfo = meminfo
        self.busy_html = BUSY_PAGE.read_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and scope.get("path", "").endswith(STREAM_PATH):
            self.sessions += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self.sessions -= 1
            return
        if scope["type"] == "http" and self._is_page_load(scope):
            reason = busy_reason(self.sessions, free_mb(self.meminfo), self.max_sessions,
                                 self.min_free_mb)
            if reason:
                await send({"type": "http.response.start", "status": 503, "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(self.busy_html)).encode()),
                    (b"cache-control", b"no-store"), (b"retry-after", b"20"),
                    (b"x-smart-garden-busy", reason.encode())]})
                await send({"type": "http.response.body", "body": self.busy_html})
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_page_load(scope) -> bool:
        if scope.get("method") != "GET":
            return False
        path = scope.get("path", "/")
        accept = dict(scope.get("headers") or []).get(b"accept", b"")
        return b"text/html" in accept and (path == "/" or "." not in path.rsplit("/", 1)[-1])


class StaticCacheMiddleware:
    """Mark Streamlit's built JS, CSS, fonts and images as cacheable for a year.

    Their file names carry a content hash (index.3f9a1c.js), so a new Streamlit
    version gets new names and nothing goes stale. With this header Cloudflare
    keeps them at its edge, so a visitor's first load (2-3 MB of scripts) comes
    from the nearest Cloudflare server instead of through the tunnel from the Pi.
    """

    CACHEABLE = (".js", ".css", ".woff", ".woff2", ".ttf", ".svg", ".png", ".ico", ".map")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or "/static/" not in path
                or not path.endswith(self.CACHEABLE)):
            await self.app(scope, receive, send)
            return

        async def with_cache(message):
            if message["type"] == "http.response.start" and message.get("status") == 200:
                headers = [(k, v) for k, v in message.get("headers") or []
                           if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_cache)
