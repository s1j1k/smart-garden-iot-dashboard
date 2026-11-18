"""Public-site load guard: busy page for new visitors when the Pi is full."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import load_guard  # noqa: E402


def _meminfo(tmp_path: Path, mb: int) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal: 3800000 kB\nMemAvailable: {mb * 1024} kB\n")
    return path


def test_busy_reason():
    assert load_guard.busy_reason(10, 1000, 60, 350) is None
    assert "visitors" in load_guard.busy_reason(60, 1000, 60, 350)
    assert "MB free" in load_guard.busy_reason(5, 200, 60, 350)
    assert load_guard.busy_reason(5, None, 60, 350) is None      # no /proc/meminfo


def test_free_mb(tmp_path):
    assert load_guard.free_mb(_meminfo(tmp_path, 512)) == 512
    assert load_guard.free_mb(tmp_path / "missing") is None


def _run(guard, scope):
    sent = []

    async def receive():
        return {"type": "websocket.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    return sent


def _page(path="/"):
    return {"type": "http", "method": "GET", "path": path,
            "headers": [(b"accept", b"text/html,application/xhtml+xml")]}


def test_guard_counts_sessions_and_serves_the_busy_page(tmp_path):
    passed = []
    gate = asyncio.Event()

    async def app(scope, receive, send):
        passed.append(scope["path"])
        if scope["type"] == "websocket":
            await gate.wait()

    guard = load_guard.LoadGuardMiddleware(app, max_sessions=2, min_free_mb=100,
                                           meminfo=_meminfo(tmp_path, 900))

    async def scenario():
        async def noop(*_):
            return {"type": "websocket.disconnect"}
        ws = {"type": "websocket", "path": "/_stcore/stream", "headers": []}
        tasks = [asyncio.create_task(guard(ws, noop, noop)) for _ in range(2)]
        await asyncio.sleep(0)
        assert guard.sessions == 2
        sent = []

        async def send(message):
            sent.append(message)
        await guard(_page(), noop, send)                       # full: busy page
        assert sent[0]["status"] == 503 and b"high load" in sent[1]["body"]
        sent.clear()
        await guard(_page("/static/js/main.js"), noop, send)  # assets still pass
        gate.set()
        await asyncio.gather(*tasks)
        assert guard.sessions == 0
        await guard(_page(), noop, send)                      # room again: the app
    asyncio.run(scenario())
    assert passed.count("/") == 1 and "/static/js/main.js" in passed


def test_low_memory_also_turns_visitors_away(tmp_path):
    async def app(scope, receive, send):
        raise AssertionError("should not reach the app")

    guard = load_guard.LoadGuardMiddleware(app, max_sessions=60, min_free_mb=350,
                                           meminfo=_meminfo(tmp_path, 200))
    sent = _run(guard, _page())
    assert sent[0]["status"] == 503
    assert dict(sent[0]["headers"])[b"x-smart-garden-busy"] == b"200 MB free"


def test_static_files_get_a_long_cache_header():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"cache-control", b"no-cache")]})
        await send({"type": "http.response.body", "body": b"x"})

    guard = load_guard.StaticCacheMiddleware(app)
    sent = _run(guard, {"type": "http", "method": "GET", "path": "/static/js/index.abc123.js",
                        "headers": []})
    assert dict(sent[0]["headers"])[b"cache-control"] == b"public, max-age=31536000, immutable"
    page = _run(guard, {"type": "http", "method": "GET", "path": "/", "headers": []})
    assert dict(page[0]["headers"])[b"cache-control"] == b"no-cache"
