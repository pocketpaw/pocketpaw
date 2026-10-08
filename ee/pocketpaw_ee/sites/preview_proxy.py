# ee/pocketpaw_ee/sites/preview_proxy.py: the preview origin's reverse proxy to a
# project draft's account-level draft Worker (``draft_worker``).
#
# ``preview_origin.preview_app`` calls ``forward`` only for a token whose draft is
# ``live`` and deployed at that token's content hash, so the capability URL
# ``<token>.<preview base>`` stays the only address anyone sees; the workers.dev host
# never reaches a browser (``Location`` is rewritten, the upstream is never named in
# an error).
#
# Rules a reader must not break:
#   * the upstream host comes from the registry only, never from the request: a
#     request target that does not start with ``/`` is a 400, the URL is built from
#     parts, and its host is checked before anything is sent;
#   * the proxy sends the draft's ``X-Paw-Draft-Key`` (the Worker's guard refuses
#     anyone else) and never forwards one a client sent;
#   * every method is forwarded (OPTIONS too: the Worker owns its CORS); the request
#     body streams through under ``PAW_SITES_DRAFT_MAX_BODY`` (413 past it);
#   * cookies: every Set-Cookie comes back as a host-only ``__Host-`` cookie (``Path=/;
#     Secure; SameSite=None; Partitioned``, Domain dropped): an app's own ``__Host-``
#     names stay, any other name ``n`` becomes ``__Host-paw~n``, and a native name in
#     the ``__Host-paw~`` space is dropped so the mapping stays one-to-one. Only
#     ``__Host-`` cookies go up (``__Host-paw~n`` as ``n``), so a cookie a sibling draft
#     tossed with ``Domain=<preview base>`` never reaches this draft. This is the ONLY
#     place the preview origin passes cookies; static drafts never do;
#   * HTML responses are decoded and streamed with the runtime reporter injected at
#     the first ``<head>`` and, under ``?paw_edit=1`` (never forwarded), the edit
#     bridge appended. Everything else streams raw with its own encoding.
# ``forward_ws`` proxies a WebSocket under the same target, header and cookie rules,
# plus: the browser ``Origin`` must be exactly the draft's own preview origin (else
# close 4403); nothing is accepted downstream until the upstream handshake works;
# caps on message size, connections per token, browser message rate, draft byte
# rate, browser idle time and lifetime close with 1009 / 1013 / 1008 / 1008 / 1001 /
# 1001. Draft messages never reset the idle timer. The per-token count is
# in-process, so the real ceiling is that cap times the number of API replicas.
# The ASGI server buffers a whole frame (uvicorn ``ws_max_size``, 16 MiB) before the
# size cap here sees it; the per-token cap bounds that exposure.
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as _ws_connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

MAX_BODY_ENV = "PAW_SITES_DRAFT_MAX_BODY"
DEFAULT_MAX_BODY = 10 * 1024 * 1024
_HEAD_LOOKAHEAD = 64 * 1024

_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_IN = _HOP | {
    "host",
    "x-real-ip",
    "forwarded",
    "true-client-ip",
    "accept-encoding",
    "cookie",
    "x-paw-draft-key",
}
_DROP_IN_PREFIXES = ("cf-", "x-forwarded-")
_COOKIE_FLAGS = {"domain", "samesite", "secure", "partitioned", "path"}

HOST_PREFIX = "__Host-"
#: Plain upstream cookie names travel to the browser under this prefix.
PROXY_PREFIX = "__Host-paw~"

_HEAD_RE = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)

_transport: httpx.AsyncBaseTransport | None = None
_clients: dict[int, httpx.AsyncClient] = {}


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Test seam: route upstream calls through ``transport`` (None: the network)."""
    global _transport
    _transport = transport
    _clients.clear()


def _client() -> httpx.AsyncClient:
    loop = id(asyncio.get_running_loop())
    client = _clients.get(loop)
    if client is None:
        client = httpx.AsyncClient(
            transport=_transport,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        )
        _clients[loop] = client
    return client


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _max_body() -> int:
    return _env_int(MAX_BODY_ENV, DEFAULT_MAX_BODY)


def builder_origin_for(pocket_id: str) -> str:
    """The origin the injected reporter / bridge post to (same rule as the editor)."""
    from pocketpaw_ee.sites import service as sites_service

    return sites_service._recorded_view_origin(pocket_id) or sites_service._builder_origin()


def rewrite_set_cookie(value: str, *, secure: bool = True) -> str | None:
    """An upstream Set-Cookie as the host-only ``__Host-`` cookie the browser gets,
    or ``None`` to drop it (malformed, or a native name inside ``__Host-paw~``).
    ``__Host-`` cookies are always ``Secure``; browsers accept that on https and on
    ``*.localhost``, so ``secure`` is informational."""
    parts = [p.strip() for p in value.split(";")]
    name, sep, val = parts[0].partition("=")
    name = name.strip()
    if not sep or not name or name.startswith(PROXY_PREFIX):
        return None
    if not name.startswith(HOST_PREFIX):
        name = PROXY_PREFIX + name
    attrs = [a for a in parts[1:] if a]
    kept = [a for a in attrs if a.split("=", 1)[0].strip().lower() not in _COOKIE_FLAGS]
    return "; ".join([f"{name}={val}", "Path=/", *kept, "Secure", "SameSite=None", "Partitioned"])


def request_cookies(header: str) -> str:
    """The Cookie header the draft Worker gets: ``__Host-`` cookies only, the proxy's
    ``__Host-paw~n`` sent back as ``n``. Anything else (a cookie another draft tossed
    with a Domain attribute) is dropped."""
    out: list[str] = []
    for part in header.split(";"):
        name, sep, val = part.strip().partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        if name.startswith(PROXY_PREFIX):
            name = name[len(PROXY_PREFIX) :]
            if not name:
                continue
        elif not name.startswith(HOST_PREFIX):
            continue
        out.append(f"{name}={val}")
    return "; ".join(out)


class _TooLarge(Exception):
    pass


class _HeadInjector:
    """Inserts ``tag`` right after the first ``<head>``; with none in the first 64 KiB
    (or the whole page), after the doctype, else at the start."""

    def __init__(self, tag: bytes) -> None:
        self.tag = tag
        self.buf = b""
        self.done = False

    def feed(self, chunk: bytes) -> bytes:
        if self.done:
            return chunk
        self.buf += chunk
        m = _HEAD_RE.search(self.buf)
        if m:
            return self._emit(m.end())
        return self._fallback() if len(self.buf) >= _HEAD_LOOKAHEAD else b""

    def flush(self) -> bytes:
        return b"" if self.done else self._fallback()

    def _fallback(self) -> bytes:
        m = _DOCTYPE_RE.search(self.buf)
        at = m.end() if m and not self.buf[: m.start()].strip() else 0
        return self._emit(at)

    def _emit(self, at: int) -> bytes:
        self.done = True
        out = self.buf[:at] + self.tag + self.buf[at:]
        self.buf = b""
        return out


async def _plain(send: Any, status: int, text: str, head: bool) -> None:
    from pocketpaw_ee.sites.preview_origin import _BASE_HEADERS, _send

    headers = list(_BASE_HEADERS) + [
        ("content-type", "text/plain; charset=utf-8"),
        ("cache-control", "no-store"),
    ]
    await _send(send, status, text.encode(), headers, head)


async def forward(
    scope: dict[str, Any], receive: Any, send: Any, *, target: Any, preview_host: str
) -> None:
    """Proxy one request to ``target.host`` and stream the answer back."""
    from pocketpaw_ee.sites import preview_origin

    started = time.monotonic()
    method = scope.get("method", "GET").upper()
    head = method == "HEAD"
    scheme = urlsplit(preview_origin.preview_base_url()).scheme or "https"
    label = preview_host[:6]

    query = scope.get("query_string", b"").decode("latin-1")
    pairs = [p for p in query.split("&") if p and p.split("=", 1)[0] != "paw_edit"]
    edit = "paw_edit=1" in query.split("&")
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode()
    path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else str(raw_path)
    path = path.split("?", 1)[0]  # some servers put the query in raw_path
    # Only an origin-form target ("/..."). Anything else ("@evil.com/x", ":x@10.0.0.1/")
    # could turn the registry host into userinfo of a URL pointing elsewhere.
    if not path.startswith("/"):
        await _plain(send, 400, "Bad request", head)
        return
    target_raw = path + (f"?{'&'.join(pairs)}" if pairs else "")
    try:
        url = httpx.URL(scheme="https", host=target.host, raw_path=target_raw.encode("latin-1"))
    except Exception:  # noqa: BLE001 - an unparsable target is the client's error
        url = None
    if url is None or url.host != target.host:
        await _plain(send, 400, "Bad request", head)
        return

    headers: list[tuple[str, str]] = []
    cookies: list[str] = []
    accepts_gzip = False
    length = None
    for k, v in scope.get("headers") or []:
        name = k.decode("latin-1").lower()
        value = v.decode("latin-1")
        if name == "accept-encoding":
            accepts_gzip = "gzip" in value.lower()
        if name == "content-length":
            length = value
        if name == "cookie":
            cookies.append(value)
        if name in _DROP_IN or name.startswith(_DROP_IN_PREFIXES):
            continue
        headers.append((name, value))
    cookie = request_cookies("; ".join(cookies))
    if cookie:
        headers.append(("cookie", cookie))
    headers += [
        ("x-forwarded-host", preview_host),
        ("x-forwarded-proto", scheme),
        ("accept-encoding", "gzip" if accepts_gzip else "identity"),
        ("x-paw-draft-key", target.key),
    ]

    cap = _max_body()
    if length is not None and length.isdigit() and int(length) > cap:
        await _plain(send, 413, "Request body too large", head)
        return

    async def _body():
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > cap:
                raise _TooLarge
            if chunk:
                yield chunk
            if not message.get("more_body"):
                return

    content = None if method in ("GET", "HEAD") else _body()
    client = _client()
    try:
        request = client.build_request(method, url, headers=headers, content=content)
        resp = await client.send(request, stream=True)
    except _TooLarge:
        await _plain(send, 413, "Request body too large", head)
        return
    except httpx.HTTPError as exc:
        logger.info(
            "sites.preview_proxy %s…: draft worker unreachable (%s)", label, type(exc).__name__
        )
        await _plain(send, 502, "The draft is not reachable right now.", head)
        return

    try:
        ctype = resp.headers.get("content-type", "").lower()
        html = "text/html" in ctype and not head and resp.status_code not in (204, 304)
        base = preview_origin._BASE_HEADERS
        base_names = {k for k, _v in base}
        out: list[tuple[str, str]] = [(k, v) for k, v in base if k != "access-control-allow-origin"]
        for k, v in resp.headers.multi_items():
            name = k.lower()
            if name in _HOP or (name in base_names and name != "access-control-allow-origin"):
                continue
            if html and name in ("content-length", "content-encoding"):
                continue
            if name == "set-cookie":
                rewritten = rewrite_set_cookie(v, secure=scheme == "https")
                if rewritten is None:
                    continue
                v = rewritten
            elif name == "location":
                loc = urlsplit(v)
                if loc.hostname and loc.hostname.lower() == target.host.lower():
                    v = loc._replace(scheme=scheme, netloc=preview_host).geturl()
            out.append((name, v))
        await send(
            {
                "type": "http.response.start",
                "status": resp.status_code,
                "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out],
            }
        )
        if head:
            await send({"type": "http.response.body", "body": b""})
            return
        if html:
            origin = await asyncio.to_thread(builder_origin_for, target.pocket_id)
            reporter = (
                f'<script id="{preview_origin._REPORTER_SCRIPT_ID}">'
                f"{preview_origin.runtime_reporter_script(origin)}</script>"
            ).encode()
            injector = _HeadInjector(reporter)
            async for chunk in resp.aiter_bytes():
                data = injector.feed(chunk)
                if data:
                    await send({"type": "http.response.body", "body": data, "more_body": True})
            tail = injector.flush()
            if edit:
                tail += (
                    f'<script id="{preview_origin._BRIDGE_SCRIPT_ID}">\n'
                    f"{preview_origin.edit_bridge_script(origin)}\n</script>"
                ).encode()
            await send({"type": "http.response.body", "body": tail})
        else:
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
    except httpx.HTTPError as exc:
        # Headers are out; all that is left is to end the body.
        logger.info(
            "sites.preview_proxy %s…: upstream stream broke (%s)", label, type(exc).__name__
        )
        await send({"type": "http.response.body", "body": b""})
    finally:
        await resp.aclose()
        logger.debug(
            "sites.preview_proxy %s… %s %s -> %s in %.0f ms",
            label,
            method,
            path[:80],
            resp.status_code,
            (time.monotonic() - started) * 1000,
        )


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------

WS_MAX_MSG_ENV = "PAW_SITES_DRAFT_WS_MAX_MSG"
WS_PER_TOKEN_ENV = "PAW_SITES_DRAFT_WS_PER_TOKEN"
WS_RATE_ENV = "PAW_SITES_DRAFT_WS_RATE"
WS_IDLE_ENV = "PAW_SITES_DRAFT_WS_IDLE_SECONDS"
WS_LIFETIME_ENV = "PAW_SITES_DRAFT_WS_LIFETIME_SECONDS"
DEFAULT_WS_MAX_MSG = 64 * 1024
DEFAULT_WS_PER_TOKEN = 60
DEFAULT_WS_RATE = 50  # browser -> draft messages per second, per connection
DEFAULT_WS_IDLE = 5 * 60
DEFAULT_WS_LIFETIME = 60 * 60
#: Draft -> browser byte budget: a token bucket refilled at this rate, holding up to
#: the burst (never less than one max-size message).
WS_DOWN_RATE_ENV = "PAW_SITES_DRAFT_WS_DOWN_BYTES_PER_SEC"
WS_DOWN_BURST_ENV = "PAW_SITES_DRAFT_WS_DOWN_BURST"
DEFAULT_WS_DOWN_RATE = 256 * 1024
DEFAULT_WS_DOWN_BURST = 1024 * 1024

CLOSE_GOING_AWAY = 1001  # idle or lifetime reached
CLOSE_POLICY = 1008  # bad request target, message rate
CLOSE_TOO_BIG = 1009
CLOSE_UPSTREAM = 1011
CLOSE_BUSY = 1013  # per-token connection cap
CLOSE_FORBIDDEN = 4403  # foreign Origin
CLOSE_NOT_FOUND = 4404  # unknown, static or superseded draft

#: The HTTP status a pre-accept refusal becomes when the server supports the
#: ``websocket.http.response`` extension.
_DENY_STATUS = {
    CLOSE_POLICY: 400,
    CLOSE_FORBIDDEN: 403,
    CLOSE_NOT_FOUND: 404,
    CLOSE_BUSY: 429,
    CLOSE_UPSTREAM: 502,
}
_DROP_IN_WS = _DROP_IN | {
    "origin",
    "content-length",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
    "sec-websocket-accept",
}

#: Seconds between idle / lifetime checks; ``_now`` is the clock they read.
_WATCH_TICK = 1.0
_now = time.monotonic

#: Open proxied WebSockets per preview token, in THIS process only.
_ws_active: dict[str, int] = {}


def _default_dial(
    uri: str,
    *,
    headers: list[tuple[str, str]],
    origin: str,
    subprotocols: list[str],
    max_size: int,
) -> Any:
    return _ws_connect(
        uri,
        additional_headers=headers,
        origin=origin,  # type: ignore[arg-type]
        subprotocols=subprotocols or None,  # type: ignore[arg-type]
        max_size=max_size,
        compression=None,
        user_agent_header=None,
        proxy=None,  # never an env-configured proxy: the registry host is the only peer
        open_timeout=10,
        close_timeout=5,
    )


_ws_dialer: Any = _default_dial


def set_ws_dialer(dialer: Any) -> None:
    """Test seam: open upstream WebSockets with ``dialer`` (None: the network)."""
    global _ws_dialer
    _ws_dialer = dialer or _default_dial


async def deny_ws(scope: dict[str, Any], send: Any, code: int) -> None:
    """Refuse a WebSocket before accept: an HTTP error when the server can send one,
    else ``websocket.close`` with ``code`` (the client sees a failed handshake)."""
    if "websocket.http.response" in (scope.get("extensions") or {}):
        await send(
            {
                "type": "websocket.http.response.start",
                "status": _DENY_STATUS.get(code, 403),
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "websocket.http.response.body", "body": b"Not available"})
        return
    await send({"type": "websocket.close", "code": code})


def _wire_code(code: Any, abnormal: int = CLOSE_UPSTREAM) -> int:
    """A close code that may be sent on the wire (1005 / 1006 / 1015 are reserved)."""
    if code == 1005:
        return 1000
    if isinstance(code, int) and (
        1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999
    ):
        return code
    return abnormal


def _reason(text: Any) -> str:
    """A close reason that fits a control frame (123 bytes of UTF-8)."""
    return (text or "").encode("utf-8")[:123].decode("utf-8", "ignore")


async def forward_ws(
    scope: dict[str, Any], receive: Any, send: Any, *, target: Any, preview_host: str
) -> None:
    """Proxy one WebSocket to ``target.host``. The ``websocket.connect`` message has
    already been received; nothing is accepted until the upstream handshake works."""
    from pocketpaw_ee.sites import preview_origin

    scheme = urlsplit(preview_origin.preview_base_url()).scheme or "https"
    label = preview_host[:6]

    query = scope.get("query_string", b"").decode("latin-1")
    pairs = [p for p in query.split("&") if p and p.split("=", 1)[0] != "paw_edit"]
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode()
    path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else str(raw_path)
    path = path.split("?", 1)[0]
    if not path.startswith("/"):  # the same origin-form rule as ``forward``
        await deny_ws(scope, send, CLOSE_POLICY)
        return
    target_raw = path + (f"?{'&'.join(pairs)}" if pairs else "")
    try:
        url = httpx.URL(scheme="https", host=target.host, raw_path=target_raw.encode("latin-1"))
    except Exception:  # noqa: BLE001 - an unparsable target is the client's error
        url = None
    if url is None or url.host != target.host:
        await deny_ws(scope, send, CLOSE_POLICY)
        return
    uri = f"wss://{url.host}{url.raw_path.decode('ascii')}"

    origin = ""
    headers: list[tuple[str, str]] = []
    cookies: list[str] = []
    for k, v in scope.get("headers") or []:
        name = k.decode("latin-1").lower()
        value = v.decode("latin-1")
        if name == "origin":
            origin = value.strip()
        if name == "cookie":
            cookies.append(value)
        if name in _DROP_IN_WS or name.startswith(_DROP_IN_PREFIXES):
            continue
        headers.append((name, value))
    # Only the draft's own origin: pages in the draft iframe always send it, and the
    # draft's PAW_SITE_ORIGINS holds nothing else, so the draft would refuse any
    # other Origin anyway (and a recorded view origin is whatever a client claimed).
    if origin.rstrip("/").lower() != f"{scheme}://{preview_host}".lower():
        await deny_ws(scope, send, CLOSE_FORBIDDEN)
        return
    cookie = request_cookies("; ".join(cookies))
    if cookie:
        headers.append(("cookie", cookie))
    headers += [
        ("x-forwarded-host", preview_host),
        ("x-forwarded-proto", scheme),
        ("x-paw-draft-key", target.key),
    ]

    token = preview_origin.token_from_host(preview_host) or preview_host
    if _ws_active.get(token, 0) >= _env_int(WS_PER_TOKEN_ENV, DEFAULT_WS_PER_TOKEN):
        await deny_ws(scope, send, CLOSE_BUSY)
        return
    _ws_active[token] = _ws_active.get(token, 0) + 1
    try:
        max_msg = _env_int(WS_MAX_MSG_ENV, DEFAULT_WS_MAX_MSG)
        try:
            upstream = await _ws_dialer(
                uri,
                headers=headers,
                origin=origin,
                subprotocols=[str(p) for p in scope.get("subprotocols") or []],
                max_size=max_msg,
            )
        except Exception as exc:  # noqa: BLE001 - handshake refused, DNS, timeout...
            logger.info(
                "sites.preview_proxy %s…: draft websocket unreachable (%s)",
                label,
                type(exc).__name__,
            )
            await deny_ws(scope, send, CLOSE_UPSTREAM)
            return
        try:
            accept_headers: list[tuple[bytes, bytes]] = []
            for v in upstream.response.headers.get_all("Set-Cookie"):
                rewritten = rewrite_set_cookie(v, secure=scheme == "https")
                if rewritten is not None:
                    accept_headers.append((b"set-cookie", rewritten.encode("latin-1")))
            await send(
                {
                    "type": "websocket.accept",
                    "subprotocol": upstream.subprotocol,
                    "headers": accept_headers,
                }
            )
            await _relay(receive, send, upstream, max_msg=max_msg, label=label)
        finally:
            await _close_upstream(upstream, 1000, "")
    finally:
        left = _ws_active.get(token, 1) - 1
        if left > 0:
            _ws_active[token] = left
        else:
            _ws_active.pop(token, None)


async def _relay(receive: Any, send: Any, upstream: Any, *, max_msg: int, label: str) -> None:
    """Pump messages both ways until one side closes or a cap trips, then close the
    other side with the matching code. The message-rate cap counts browser messages,
    the byte budget draft messages; only browser messages count as activity."""
    rate = _env_int(WS_RATE_ENV, DEFAULT_WS_RATE)
    down_rate = _env_int(WS_DOWN_RATE_ENV, DEFAULT_WS_DOWN_RATE)
    burst = max(_env_int(WS_DOWN_BURST_ENV, DEFAULT_WS_DOWN_BURST), max_msg)
    idle = _env_int(WS_IDLE_ENV, DEFAULT_WS_IDLE)
    lifetime = _env_int(WS_LIFETIME_ENV, DEFAULT_WS_LIFETIME)
    started = _now()
    last = [started]

    async def browser_to_draft() -> tuple[str, int, str]:
        window, count = _now(), 0
        while True:
            message = await receive()
            kind = message["type"]
            if kind == "websocket.disconnect":
                return "browser", message.get("code", 1005), message.get("reason") or ""
            if kind != "websocket.receive":
                continue
            data = message.get("text")
            if data is None:
                data = message.get("bytes") or b""
            size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
            if size > max_msg:
                return "cap", CLOSE_TOO_BIG, "message too big"
            now = _now()
            if now - window >= 1.0:
                window, count = now, 0
            count += 1
            if count > rate:
                return "cap", CLOSE_POLICY, "rate limit"
            last[0] = now
            try:
                await upstream.send(data)
            except ConnectionClosed as exc:
                return _closed(exc)

    async def draft_to_browser() -> tuple[str, int, str]:
        budget, refilled = float(burst), _now()
        while True:
            try:
                data = await upstream.recv()
            except ConnectionClosed as exc:
                return _closed(exc)
            now = _now()
            budget = min(float(burst), budget + (now - refilled) * down_rate)
            refilled = now
            budget -= len(data.encode("utf-8")) if isinstance(data, str) else len(data)
            if budget < 0:
                return "cap", CLOSE_POLICY, "draft rate limit"
            key = "text" if isinstance(data, str) else "bytes"
            await send({"type": "websocket.send", key: data})

    async def watchdog() -> tuple[str, int, str]:
        while True:
            await asyncio.sleep(_WATCH_TICK)
            now = _now()
            if now - started >= lifetime:
                return "cap", CLOSE_GOING_AWAY, "connection lifetime reached"
            if now - last[0] >= idle:
                return "cap", CLOSE_GOING_AWAY, "idle timeout"

    tasks = [
        asyncio.create_task(browser_to_draft()),
        asyncio.create_task(draft_to_browser()),
        asyncio.create_task(watchdog()),
    ]
    side, code, reason = "error", CLOSE_UPSTREAM, ""
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        side, code, reason = next(iter(done)).result()
    except Exception as exc:  # noqa: BLE001 - any failure closes both sides
        logger.info(
            "sites.preview_proxy %s…: websocket relay broke (%s)", label, type(exc).__name__
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    reason = _reason(reason)
    if side == "browser":
        await _close_upstream(upstream, _wire_code(code, abnormal=CLOSE_GOING_AWAY), reason)
        return
    if side != "draft":
        await _close_upstream(upstream, code, reason)
    try:
        await send({"type": "websocket.close", "code": _wire_code(code), "reason": reason})
    except Exception:  # noqa: BLE001 - the browser is already gone
        pass


def _closed(exc: ConnectionClosed) -> tuple[str, int, str]:
    frame = exc.rcvd or exc.sent
    return "draft", (frame.code if frame else 1006), (frame.reason if frame else "")


async def _close_upstream(upstream: Any, code: int, reason: str) -> None:
    try:
        await upstream.close(code, reason)
    except Exception:  # noqa: BLE001 - already closed or broken
        pass


__all__ = [
    "builder_origin_for",
    "deny_ws",
    "forward",
    "forward_ws",
    "rewrite_set_cookie",
    "set_transport",
    "set_ws_dialer",
]
