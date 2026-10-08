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
#   * the upstream host comes from the registry only, never from the request;
#   * every method is forwarded (OPTIONS too: the Worker owns its CORS); the request
#     body streams through under ``PAW_SITES_DRAFT_MAX_BODY`` (413 past it);
#   * Cookie goes up and Set-Cookie comes back, rewritten host-only (Domain dropped)
#     and, on https, ``Secure; SameSite=None; Partitioned`` so auth works in the
#     builder's cross-site iframe. This is the ONLY place the preview origin passes
#     cookies; static drafts never do;
#   * HTML responses are decoded and streamed with the runtime reporter injected at
#     the first ``<head>`` and, under ``?paw_edit=1`` (never forwarded), the edit
#     bridge appended. Everything else streams raw with its own encoding.
# WebSockets are not proxied.
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

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
_DROP_IN = _HOP | {"host", "x-real-ip", "forwarded", "true-client-ip", "accept-encoding"}
_DROP_IN_PREFIXES = ("cf-", "x-forwarded-")
_COOKIE_FLAGS = {"domain", "samesite", "secure", "partitioned"}

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


def _max_body() -> int:
    raw = (os.environ.get(MAX_BODY_ENV) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_BODY
    except ValueError:
        return DEFAULT_MAX_BODY
    return value if value > 0 else DEFAULT_MAX_BODY


def builder_origin_for(pocket_id: str) -> str:
    """The origin the injected reporter / bridge post to (same rule as the editor)."""
    from pocketpaw_ee.sites import service as sites_service

    return sites_service._recorded_view_origin(pocket_id) or sites_service._builder_origin()


def rewrite_set_cookie(value: str, *, secure: bool) -> str:
    """Host-only always; on https also ``Secure; SameSite=None; Partitioned``."""
    parts = [p.strip() for p in value.split(";")]
    attrs = [a for a in parts[1:] if a]
    if secure:
        kept = [a for a in attrs if a.split("=", 1)[0].strip().lower() not in _COOKIE_FLAGS]
        kept += ["Secure", "SameSite=None", "Partitioned"]
    else:
        kept = [a for a in attrs if a.split("=", 1)[0].strip().lower() != "domain"]
    return "; ".join([parts[0], *kept])


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
    secure = scheme == "https"
    label = preview_host[:6]

    query = scope.get("query_string", b"").decode("latin-1")
    pairs = [p for p in query.split("&") if p and p.split("=", 1)[0] != "paw_edit"]
    edit = "paw_edit=1" in query.split("&")
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode()
    path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else str(raw_path)
    path = path.split("?", 1)[0]  # some servers put the query in raw_path
    url = f"https://{target.host}{path or '/'}" + (f"?{'&'.join(pairs)}" if pairs else "")

    headers: list[tuple[str, str]] = []
    accepts_gzip = False
    length = None
    for k, v in scope.get("headers") or []:
        name = k.decode("latin-1").lower()
        value = v.decode("latin-1")
        if name == "accept-encoding":
            accepts_gzip = "gzip" in value.lower()
        if name == "content-length":
            length = value
        if name in _DROP_IN or name.startswith(_DROP_IN_PREFIXES):
            continue
        headers.append((name, value))
    headers += [
        ("x-forwarded-host", preview_host),
        ("x-forwarded-proto", scheme),
        ("accept-encoding", "gzip" if accepts_gzip else "identity"),
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
                v = rewrite_set_cookie(v, secure=secure)
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


__all__ = ["builder_origin_for", "forward", "rewrite_set_cookie", "set_transport"]
