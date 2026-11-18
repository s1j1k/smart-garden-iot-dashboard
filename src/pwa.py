"""Progressive-web-app shell around the Streamlit dashboard.

``server.py`` wraps ``app.py`` in ``st.App`` with these routes and middleware:

* ``/manifest.webmanifest`` — name, colours and icons for "Install app" / "Add to Home Screen"
* ``/sw.js`` — service worker at the site root (offline page, notification taps)
* ``/pwa/<file>`` — icons and the offline page from ``src/pwa_assets``
* ``PwaHeadMiddleware`` — adds the manifest link, theme colour, iOS tags and the service
  worker registration to every HTML page Streamlit serves

Browsers only install apps and run service workers on HTTPS or http://localhost.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.responses import FileResponse, Response
from starlette.routing import Route

ASSETS = Path(__file__).resolve().parent / "pwa_assets"
THEME = "#1b1530"
NAME = "Smart Garden"

MANIFEST = {
    "name": NAME,
    "short_name": "Garden",
    "description": "Live readings, alerts and grow-stage guidance for the smart garden.",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": THEME,
    "theme_color": THEME,
    "icons": [
        {"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
    "shortcuts": [
        {"name": "Alerts", "url": "/alerts", "icons": [{"src": "/pwa/icon-192.png",
                                                        "sizes": "192x192"}]},
        {"name": "Trends", "url": "/trends", "icons": [{"src": "/pwa/icon-192.png",
                                                        "sizes": "192x192"}]},
    ],
}

HEAD_TAGS = f"""
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="{THEME}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Garden">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/pwa/apple-touch-icon.png">
<script>
if ("serviceWorker" in navigator && window.isSecureContext) {{
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("/sw.js", {{scope: "/"}}).catch(() => {{}}));
}}
</script>
""".encode()

_TYPES = {".png": "image/png", ".html": "text/html; charset=utf-8",
          ".js": "text/javascript; charset=utf-8"}


async def manifest(_request):
    return Response(json.dumps(MANIFEST), media_type="application/manifest+json")


async def service_worker(_request):
    return FileResponse(ASSETS / "sw.js", media_type=_TYPES[".js"],
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


async def asset(request):
    name = request.path_params["name"]
    path = (ASSETS / name).resolve()
    if path.parent != ASSETS or not path.is_file() or path.suffix not in _TYPES:
        return Response(status_code=404)
    return FileResponse(path, media_type=_TYPES[path.suffix],
                        headers={"Cache-Control": "public, max-age=86400"})


ROUTES = [
    Route("/manifest.webmanifest", manifest),
    Route("/sw.js", service_worker),
    Route("/pwa/{name}", asset),
]


def inject_head(html: bytes) -> bytes:
    if b'rel="manifest"' in html or b"<head>" not in html:
        return html
    return html.replace(b"<head>", b"<head>" + HEAD_TAGS, 1)


class PwaHeadMiddleware:
    """Add the PWA head tags to Streamlit's HTML page (pure ASGI, so it can edit the body)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if b"text/html" not in headers.get(b"accept", b""):
            await self.app(scope, receive, send)
            return
        # Ask for an uncompressed page so the body can be edited.
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"accept-encoding"]
        start, chunks = {}, []

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            resp_headers = list(start.get("headers") or [])
            ctype = dict(resp_headers).get(b"content-type", b"")
            if b"text/html" in ctype:
                body = inject_head(body)
                resp_headers = [(k, v) for k, v in resp_headers if k.lower() != b"content-length"]
                resp_headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": resp_headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)


MOBILE_CSS = """
<style>
/* Tighter margins on phones so readings use the full width. */
@media (max-width: 640px) {
  .block-container, [data-testid="stMainBlockContainer"] {
    padding-left: 0.75rem !important; padding-right: 0.75rem !important;
    padding-top: 2.5rem !important;
  }
  h1 { font-size: 1.6rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
}
</style>
"""
