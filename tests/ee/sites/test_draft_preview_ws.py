# tests/ee/sites/test_draft_preview_ws.py: WebSocket passthrough on the draft preview
# origin (``preview_proxy.forward_ws``; design: docs/design/drafts/
# 2026-10-08-sites-durable-objects.md, workspace root, section 4 and slice 5).
#
# The browser side is a pair of ASGI queues around ``preview_app``; the draft Worker
# is a real ``websockets`` server on 127.0.0.1, reached through the proxy's dialer
# seam (the seam swaps only the address, so the URI the proxy built and the headers
# it sends are what the test sees). Token store and draft registry are in memory,
# shared with the HTTP proxy tests. Timeouts run on a fake clock.
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from pocketpaw_ee.sites import preview_origin, preview_proxy
from websockets.asyncio.server import serve

from tests.ee.sites.test_draft_preview_proxy import (
    BUILDER,
    DRAFT_KEY,
    HOST,
    OLD_TOKEN,
    STATIC_TOKEN,
    UPSTREAM,
    upstream,  # noqa: F401 - the shared draft fixture (env, token store, registry)
)

ORIGIN = f"https://{HOST}"
WAIT = 5.0


class Browser:
    """The downstream half: what the ASGI server would feed ``preview_app``."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send(self, message: dict) -> None:
        await self.outbox.put(message)

    async def next(self) -> dict:
        return await asyncio.wait_for(self.outbox.get(), WAIT)

    async def say(self, data: str | bytes) -> None:
        key = "text" if isinstance(data, str) else "bytes"
        await self.inbox.put({"type": "websocket.receive", key: data})

    async def done(self) -> None:
        assert self.task is not None
        await asyncio.wait_for(self.task, WAIT)


def _scope(
    host: str = HOST,
    *,
    path: bytes = b"/parties/room/lobby",
    query: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
    origin: str | None = ORIGIN,
    subprotocols: list[str] | None = None,
    extensions: dict | None = None,
) -> dict[str, Any]:
    raw = [("host", host)] + ([("origin", origin)] if origin else []) + (headers or [])
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "scheme": "wss",
        "path": path.decode("latin-1"),
        "raw_path": path,
        "query_string": query,
        "headers": [(k.encode(), v.encode()) for k, v in raw],
        "subprotocols": subprotocols or [],
        "extensions": extensions or {},
    }


async def _open(scope: dict[str, Any]) -> tuple[Browser, dict]:
    browser = Browser()
    await browser.inbox.put({"type": "websocket.connect"})
    browser.task = asyncio.create_task(
        preview_origin.preview_app(scope, browser.receive, browser.send)
    )
    return browser, await browser.next()


async def _echo(conn) -> None:
    async for message in conn:
        if message == "close-me":
            await conn.close(4001, "bye")
            return
        if message == "flood":
            await conn.send("x" * 200)
            continue
        await conn.send(message)


async def _pusher(conn, outq: asyncio.Queue) -> None:
    """A draft that talks on its own: sends whatever the test queues."""

    async def pump() -> None:
        while True:
            await conn.send(await outq.get())

    task = asyncio.create_task(pump())
    try:
        await conn.wait_closed()
    finally:
        task.cancel()


@pytest.fixture
async def worker(upstream):  # noqa: F811
    """A live fake draft Worker; ``worker.dialed`` records what the proxy dialed."""
    state = SimpleNamespace(requests=[], closes=[], dialed=[], handler=_echo, closed=None)
    state.closed = asyncio.Event()
    state.outq = asyncio.Queue()

    async def handler(conn) -> None:
        state.requests.append(conn.request)
        try:
            await state.handler(conn)
        finally:
            await conn.wait_closed()
            state.closes.append((conn.close_code, conn.close_reason))
            state.closed.set()

    def process_response(conn, request, response):
        response.headers["Set-Cookie"] = "sid=7; Path=/; HttpOnly; SameSite=Lax"
        return response

    def pick(conn, offered):
        return "y-room" if "y-room" in offered else None

    server = await serve(
        handler, "127.0.0.1", 0, select_subprotocol=pick, process_response=process_response
    )
    port = server.sockets[0].getsockname()[1]
    real = preview_proxy._default_dial

    def dialer(uri: str, **kw: Any):
        state.dialed.append((uri, kw))
        parts = urlsplit(uri)
        tail = parts.path + (f"?{parts.query}" if parts.query else "")
        return real(f"ws://127.0.0.1:{port}{tail}", **kw)

    preview_proxy.set_ws_dialer(dialer)
    yield state
    preview_proxy.set_ws_dialer(None)
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_relays_text_and_binary_both_ways(worker):
    browser, accept = await _open(_scope(subprotocols=["y-room"]))
    assert accept["type"] == "websocket.accept"
    assert accept["subprotocol"] == "y-room"
    await browser.say("hi")
    assert await browser.next() == {"type": "websocket.send", "text": "hi"}
    await browser.say(b"\x00\x01\xff")
    assert await browser.next() == {"type": "websocket.send", "bytes": b"\x00\x01\xff"}
    await browser.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await browser.done()


async def test_an_upstream_close_code_and_reason_reach_the_browser(worker):
    browser, _accept = await _open(_scope())
    await browser.say("close-me")
    close = await browser.next()
    assert close["type"] == "websocket.close"
    assert (close["code"], close.get("reason")) == (4001, "bye")
    await browser.done()


async def test_a_browser_close_code_reaches_the_worker(worker):
    browser, _accept = await _open(_scope())
    await browser.inbox.put({"type": "websocket.disconnect", "code": 4002, "reason": "left"})
    await browser.done()
    await asyncio.wait_for(worker.closed.wait(), WAIT)
    assert worker.closes == [(4002, "left")]


async def test_the_upstream_is_the_registry_host_with_the_draft_key(worker):
    browser, _accept = await _open(
        _scope(
            query=b"room=1&paw_edit=1",
            headers=[
                ("x-paw-draft-key", "guessed"),
                ("cookie", "__Host-paw~sid=6; __Host-own=1; tossed=1"),
                ("cf-connecting-ip", "1.2.3.4"),
                ("x-forwarded-for", "1.2.3.4"),
                ("x-forwarded-host", "evil.example"),
                ("connection", "Upgrade"),
                ("accept-language", "en"),
            ],
        )
    )
    (uri, _kw) = worker.dialed[0]
    parts = urlsplit(uri)
    assert parts.scheme == "wss" and parts.hostname == UPSTREAM
    assert parts.path == "/parties/room/lobby" and parts.query == "room=1"
    (req,) = worker.requests
    # The proxy, and only the proxy, proves to the draft Worker it is the caller.
    assert req.headers.get_all("x-paw-draft-key") == [DRAFT_KEY]
    assert req.headers["cookie"] == "sid=6; __Host-own=1"
    assert req.headers["origin"] == ORIGIN
    assert req.headers.get_all("x-forwarded-host") == [HOST]
    assert req.headers["x-forwarded-proto"] == "https"
    assert "cf-connecting-ip" not in req.headers
    assert "x-forwarded-for" not in req.headers
    assert req.headers["accept-language"] == "en"
    await browser.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await browser.done()


async def test_upstream_set_cookie_comes_back_host_only(worker):
    browser, accept = await _open(_scope())
    cookies = [v.decode() for k, v in accept.get("headers", []) if k.lower() == b"set-cookie"]
    assert cookies == ["__Host-paw~sid=7; Path=/; HttpOnly; Secure; SameSite=None; Partitioned"]
    await browser.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await browser.done()


async def test_only_the_drafts_own_preview_origin_may_connect(worker, monkeypatch):
    # Pages in the draft iframe always send the preview origin. The builder origin
    # is refused too: the recipe's PAW_SITE_ORIGINS would 403 it anyway, and a
    # recorded view origin is whatever a non-browser client claimed.
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", BUILDER)
    monkeypatch.setattr(preview_proxy, "builder_origin_for", lambda pocket_id: BUILDER)
    for origin in (BUILDER, "http://localhost:1420"):
        browser, close = await _open(_scope(origin=origin))
        assert close == {"type": "websocket.close", "code": 4403}
        await browser.done()
    assert worker.dialed == []


# ---------------------------------------------------------------------------
# Refusals (all before anything is dialed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [None, "https://evil.example", f"https://{OLD_TOKEN}.preview.paw.test", f"http://{HOST}"],
)
async def test_a_foreign_origin_is_refused_before_dialing(worker, origin):
    browser, close = await _open(_scope(origin=origin))
    assert close == {"type": "websocket.close", "code": 4403}
    assert worker.dialed == []
    await browser.done()


@pytest.mark.parametrize("token", [STATIC_TOKEN, OLD_TOKEN, "d" * 32])
async def test_static_superseded_and_unknown_tokens_are_refused(worker, token):
    browser, close = await _open(_scope(f"{token}.preview.paw.test"))
    assert close == {"type": "websocket.close", "code": 4404}
    assert worker.dialed == []
    await browser.done()


async def test_a_refusal_is_an_http_404_when_the_server_can_send_one(worker):
    scope = _scope(f"{STATIC_TOKEN}.preview.paw.test", extensions={"websocket.http.response": {}})
    browser, start = await _open(scope)
    assert start["type"] == "websocket.http.response.start" and start["status"] == 404
    body = await browser.next()
    assert body["type"] == "websocket.http.response.body"
    await browser.done()


async def test_drafts_off_refuses_every_websocket(worker, monkeypatch):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    browser, close = await _open(_scope())
    assert close == {"type": "websocket.close", "code": 4404}
    assert worker.dialed == []
    await browser.done()


@pytest.mark.parametrize("raw_path", [b"@evil.com/x", b":x@10.0.0.1/", b"evil.com/x"])
async def test_an_ssrf_shaped_target_is_refused(worker, raw_path):
    browser, close = await _open(_scope(path=raw_path))
    assert close == {"type": "websocket.close", "code": 1008}
    assert worker.dialed == []
    await browser.done()


async def test_an_unreachable_worker_refuses_the_browser(worker):
    def broken(uri: str, **kw: Any):
        raise OSError("connection refused")

    preview_proxy.set_ws_dialer(broken)
    browser, close = await _open(_scope())
    assert close == {"type": "websocket.close", "code": 1011}
    await browser.done()
    # The slot is given back.
    assert preview_proxy._ws_active == {}


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


async def test_an_oversized_browser_message_closes_both_sides(worker, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_MAX_MSG", "16")
    browser, _accept = await _open(_scope())
    await browser.say("x" * 17)
    close = await browser.next()
    assert close["type"] == "websocket.close" and close["code"] == 1009
    await browser.done()
    await asyncio.wait_for(worker.closed.wait(), WAIT)
    assert worker.closes[0][0] == 1009


async def test_an_oversized_worker_message_closes_both_sides(worker, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_MAX_MSG", "64")
    browser, _accept = await _open(_scope())
    await browser.say("flood")
    close = await browser.next()
    assert close["type"] == "websocket.close" and close["code"] == 1009
    await browser.done()


async def test_the_per_token_cap_refuses_and_frees_its_slot(worker, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_PER_TOKEN", "1")
    first, accept = await _open(_scope())
    assert accept["type"] == "websocket.accept"
    second, close = await _open(_scope())
    assert close == {"type": "websocket.close", "code": 1013}
    await second.done()
    assert len(worker.dialed) == 1
    await first.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await first.done()
    third, accept = await _open(_scope())
    assert accept["type"] == "websocket.accept"
    await third.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await third.done()
    assert preview_proxy._ws_active == {}


async def test_the_message_rate_cap_closes_the_connection(worker, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_RATE", "3")
    monkeypatch.setattr(preview_proxy, "_now", lambda: 1000.0)
    browser, _accept = await _open(_scope())
    for i in range(4):
        await browser.say(f"m{i}")
    message = await browser.next()
    while message["type"] == "websocket.send":  # echoes that beat the close
        message = await browser.next()
    assert message == {"type": "websocket.close", "code": 1008, "reason": "rate limit"}
    await browser.done()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(preview_proxy, "_now", lambda: now[0])
    monkeypatch.setattr(preview_proxy, "_WATCH_TICK", 0.01)
    return now


async def test_an_idle_connection_is_closed(worker, clock):
    browser, _accept = await _open(_scope())
    await browser.say("hi")
    assert (await browser.next())["text"] == "hi"
    clock[0] += 299
    await asyncio.sleep(0.05)
    assert browser.outbox.empty()
    clock[0] += 2
    close = await browser.next()
    assert close["type"] == "websocket.close" and close["code"] == 1001
    await browser.done()
    await asyncio.wait_for(worker.closed.wait(), WAIT)
    assert worker.closes[0][0] == 1001


async def test_draft_traffic_alone_does_not_keep_a_connection_alive(worker, clock):
    worker.handler = lambda conn: _pusher(conn, worker.outq)
    browser, _accept = await _open(_scope())
    for _ in range(3):
        clock[0] += 120
        await worker.outq.put("tick")
        message = await browser.next()
        if message["type"] == "websocket.close":
            break
        assert message == {"type": "websocket.send", "text": "tick"}
    else:
        message = await browser.next()
    # 360 s with only the draft talking: the browser has been idle past 300 s.
    assert message["type"] == "websocket.close" and message["code"] == 1001
    await browser.done()


async def test_the_draft_byte_budget_refills_and_then_closes_a_flood(worker, clock, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_MAX_MSG", "100")
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_DOWN_BYTES_PER_SEC", "100")
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_DOWN_BURST", "150")
    worker.handler = lambda conn: _pusher(conn, worker.outq)
    browser, _accept = await _open(_scope())
    await worker.outq.put("a" * 100)
    assert (await browser.next())["text"] == "a" * 100
    clock[0] += 1  # 50 left + 100 refilled
    await worker.outq.put("b" * 100)
    assert (await browser.next())["text"] == "b" * 100
    await worker.outq.put("c" * 100)  # 50 left: over budget
    close = await browser.next()
    assert close == {"type": "websocket.close", "code": 1008, "reason": "draft rate limit"}
    await browser.done()
    await asyncio.wait_for(worker.closed.wait(), WAIT)
    assert worker.closes[0][0] == 1008


async def test_a_busy_connection_still_ends_at_its_lifetime(worker, clock, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_WS_LIFETIME_SECONDS", "600")
    browser, _accept = await _open(_scope())
    for _ in range(3):
        clock[0] += 199
        await browser.say("ping")
        assert (await browser.next())["text"] == "ping"
    clock[0] += 10
    close = await browser.next()
    assert close["type"] == "websocket.close" and close["code"] == 1001
    await browser.done()
    assert preview_proxy._ws_active == {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_dispatch_sends_preview_websockets_to_the_preview_app(worker):
    inner_calls: list[dict] = []

    async def inner(scope, receive, send):
        inner_calls.append(scope)

    app = preview_origin.PreviewHostDispatch(inner)
    browser = Browser()
    await browser.inbox.put({"type": "websocket.connect"})
    browser.task = asyncio.create_task(app(_scope(), browser.receive, browser.send))
    assert (await browser.next())["type"] == "websocket.accept"
    await browser.inbox.put({"type": "websocket.disconnect", "code": 1000})
    await browser.done()
    assert inner_calls == []
    await app(_scope("api.paw.example"), browser.receive, browser.send)
    assert len(inner_calls) == 1
