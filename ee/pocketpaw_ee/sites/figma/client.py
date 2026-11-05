# ee/pocketpaw_ee/sites/figma/client.py: the Figma read facade for the builder agent.
#
# ``FigmaSource`` is the contract the agent tools call: file metadata, a page /
# top-frame index, depth-limited nodes, renders and local variables. Today the only
# implementation is ``RestFigmaSource`` over ``api.figma.com/v1`` with the
# workspace's ``figma`` vault grant; a client for Figma's remote MCP can replace it
# once Figma admits us to its MCP catalog, without changing the tools. Never spoof
# a catalog client name to get there sooner.
#
# Invariants a reader must not break:
#   * requests go only to ``API``; the file key and node ids are validated before
#     they reach a path or a query (``urls.py`` and ``_ids``);
#   * the token comes from ``vault.get_valid_token`` per request and is never in a
#     result, an error or a log line. A 401, or Figma's 403 "Invalid token", gets
#     one refresh through ``get_token(rejected=...)``, then counts as a reconnect;
#   * never the whole file: ``file_index`` is ``/files/:key?depth=2`` (pages and
#     their top-level frames) and ``nodes`` is ``/files/:key/nodes`` with a depth;
#   * 429 honours ``Retry-After``: a short wait is retried once, a longer one sets a
#     cooldown per connection so no call reaches Figma until it passes, and the
#     error carries ``X-Figma-Upgrade-Link`` (figma.com only). The plan-tier and
#     rate-limit-type headers go to ``on_limits`` (stored on the connection's
#     meta). 403 / 404 on a file is ``not_shared``;
#   * tier-1 answers (index, nodes, renders) are cached 24 h per connection, file
#     and the file's ``last_touched_at`` from ``/files/:key/meta`` (tier 3, cached 10
#     min), so an edited file is read fresh and an unchanged one costs no tier-1
#     call. Redis when present, else a bounded in-process dict.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from pocketpaw_ee.integrations.providers import oauth_http

logger = logging.getLogger(__name__)

API = "https://api.figma.com/v1"
#: Hosts Figma serves renders and image fills from, matched exactly.
FIGMA_IMAGE_HOSTS = frozenset(
    {
        "figma-alpha-api.s3.us-west-2.amazonaws.com",
        "s3-alpha-sig.figma.com",
        "s3-alpha.figma.com",
    }
)
CACHE_TTL = 24 * 3600
META_TTL = 600
#: A Retry-After at or under this is waited out once; longer sets a cooldown.
SHORT_WAIT_S = 5
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_CACHED_BYTES = 2 * 1024 * 1024
MAX_MEMORY_ENTRIES = 256
MAX_IDS = 20
_ID_RE = re.compile(r"I?\d{1,10}:\d{1,10}(?:;\d{1,10}:\d{1,10}){0,8}")
_KEY_RE = re.compile(r"[A-Za-z0-9]{10,64}")

NOT_SHARED = (
    "Figma refused that file. Share it with the Figma account connected here (or open "
    "it once while signed in to that account), or reconnect Figma, then try again."
)
RECONNECT = "The Figma connection has expired. Reconnect Figma in Integrations, then try again."


class FigmaError(Exception):
    """A Figma read that did not happen. ``message`` is safe to show the owner."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
        upgrade_link: str = "",
    ) -> None:
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.upgrade_link = upgrade_link
        super().__init__(f"{code}: {message}")


class FigmaSource(Protocol):
    async def file_meta(self, key: str) -> dict[str, Any]: ...
    async def file_index(self, key: str) -> dict[str, Any]: ...
    async def nodes(self, key: str, ids: list[str], depth: int) -> dict[str, Any]: ...
    async def render(self, key: str, ids: list[str], fmt: str, scale: float) -> dict[str, str]: ...
    async def variables(self, key: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------- cache

_memory: dict[str, tuple[float, str]] = {}


def clear_cache() -> None:
    _memory.clear()


def _redis():
    try:
        from pocketpaw_ee.cloud._core.redis_client import get_redis

        return get_redis()
    except Exception:  # noqa: BLE001 - no Redis configured
        return None


async def _cache_get(name: str) -> str | None:
    client = _redis()
    if client is not None:
        try:
            return await client.get(name)
        except Exception:  # noqa: BLE001
            logger.debug("figma: cache read failed", exc_info=True)
    hit = _memory.get(name)
    if hit is None or hit[0] <= time.monotonic():
        return None
    return hit[1]


async def _cache_set(name: str, value: str, ttl: int) -> None:
    if len(value) > MAX_CACHED_BYTES:
        return
    client = _redis()
    if client is not None:
        try:
            await client.set(name, value, ex=max(1, ttl))
            return
        except Exception:  # noqa: BLE001
            logger.debug("figma: cache write failed", exc_info=True)
    now = time.monotonic()
    if len(_memory) >= MAX_MEMORY_ENTRIES:
        for stale in [k for k, (until, _) in _memory.items() if until <= now]:
            _memory.pop(stale, None)
        while len(_memory) >= MAX_MEMORY_ENTRIES:
            _memory.pop(next(iter(_memory)))
    _memory[name] = (now + ttl, value)


# ---------------------------------------------------------------- REST


def _ids(ids: list[str]) -> list[str]:
    out = [i for i in dict.fromkeys(str(i).strip() for i in ids) if i]
    if not out or len(out) > MAX_IDS or not all(_ID_RE.fullmatch(i) for i in out):
        raise FigmaError("invalid_args", "Node ids must look like `12:34` (at most 20).")
    return out


def _file_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key or ""):
        raise FigmaError("invalid_args", "That is not a Figma file key.")
    return key


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(1, min(int(resp.headers.get("retry-after", "60")), 7 * 24 * 3600))
    except ValueError:
        return 60


def _upgrade_link(raw: str) -> str:
    """Figma's upgrade page, only when it really is on figma.com over https."""
    from urllib.parse import urlsplit

    link = raw.strip()[:300]
    try:
        parts = urlsplit(link)
    except ValueError:
        return ""
    if parts.scheme != "https" or (parts.hostname or "") not in ("figma.com", "www.figma.com"):
        return ""
    return link


def _token_refused(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    if resp.status_code != 403:
        return False
    err = str(oauth_http.body_of(resp).get("err") or "").lower()
    return "token" in err


class RestFigmaSource:
    """``FigmaSource`` over the REST API with one workspace grant."""

    def __init__(
        self,
        get_token: Callable[..., Awaitable[str]],
        *,
        scope: str,
        on_limits: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        self._get_token = get_token
        self._scope = scope
        self._on_limits = on_limits

    # -- the wire
    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        cool = f"figma:cool:{self._scope}"
        held = await _cache_get(cool)
        if held:
            data = json.loads(held)
            left = max(1, int(data.get("until", 0) - time.time()))
            raise FigmaError(
                "rate_limited",
                f"Figma's rate limit for this account resets in about {left} seconds.",
                retry_after=left,
                upgrade_link=str(data.get("upgrade_link") or ""),
            )
        resp = await self._send(path, params)
        if resp.status_code == 429:
            wait = _retry_after(resp)
            if wait <= SHORT_WAIT_S:
                await asyncio.sleep(wait)
                resp = await self._send(path, params)
        await self._limits(resp)
        if resp.status_code == 429:
            wait = _retry_after(resp)
            link = _upgrade_link(resp.headers.get("x-figma-upgrade-link", ""))
            await _cache_set(
                cool, json.dumps({"until": time.time() + wait, "upgrade_link": link}), wait
            )
            raise FigmaError(
                "rate_limited",
                f"Figma's rate limit for this account was reached. Try again in about "
                f"{wait} seconds.",
                retry_after=wait,
                upgrade_link=link,
            )
        if _token_refused(resp):
            raise FigmaError("needs_reconnect", RECONNECT)
        if resp.status_code in (403, 404):
            raise FigmaError("not_shared", NOT_SHARED)
        if resp.status_code >= 400:
            raise FigmaError("unavailable", f"Figma answered {resp.status_code}. Try again later.")
        if len(resp.content) > MAX_JSON_BYTES:
            raise FigmaError(
                "too_large", "That part of the file is too large to read; pick one frame."
            )
        body = oauth_http.body_of(resp)
        if body.get("err"):
            raise FigmaError("unavailable", "Figma could not answer that request.")
        return body

    async def _send(self, path: str, params: dict[str, str] | None) -> httpx.Response:
        token = await self._get_token()
        async with oauth_http.client() as http:
            for attempt in (0, 1):
                try:
                    resp = await http.get(
                        f"{API}{path}",
                        params=params,
                        headers={"authorization": f"Bearer {token}"},
                    )
                except httpx.HTTPError as exc:
                    raise FigmaError(
                        "unavailable", f"Figma is unreachable ({type(exc).__name__})."
                    ) from None
                if attempt or not _token_refused(resp):
                    return resp
                token = await self._get_token(rejected=token)
        raise FigmaError("unavailable", "Figma is unreachable.")  # pragma: no cover

    async def _limits(self, resp: httpx.Response) -> None:
        if self._on_limits is None:
            return
        seen = {
            "plan_tier": resp.headers.get("x-figma-plan-tier", ""),
            "rate_limit_type": resp.headers.get("x-figma-rate-limit-type", ""),
        }
        seen = {k: v[:20] for k, v in seen.items() if re.fullmatch(r"[a-z_]{1,20}", v or "")}
        if seen:
            try:
                await self._on_limits(seen)
            except Exception:  # noqa: BLE001 - a missed hint never fails a read
                logger.debug("figma: limits hook failed", exc_info=True)

    # -- caching
    async def _cached(
        self, kind: str, key: str, params: dict[str, str], fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        version = str((await self.file_meta(key)).get("last_touched_at") or "")
        sig = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        name = f"figma:c:{self._scope}:{key}:{kind}:{version}:{sig}"
        hit = await _cache_get(name)
        if hit is not None:
            return json.loads(hit)
        value = await fetch()
        if version:
            await _cache_set(name, json.dumps(value), CACHE_TTL)
        return value

    # -- the contract
    async def file_meta(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        name = f"figma:m:{self._scope}:{key}"
        hit = await _cache_get(name)
        if hit is not None:
            return json.loads(hit)
        body = await self._get(f"/files/{quote(key)}/meta")
        meta = body.get("file") if isinstance(body.get("file"), dict) else {}
        value = {
            "name": str(meta.get("name") or ""),
            "last_touched_at": str(meta.get("last_touched_at") or ""),
            "version": str(meta.get("version") or ""),
            "role": str(meta.get("role") or ""),
            "editor_type": str(meta.get("editorType") or ""),
        }
        await _cache_set(name, json.dumps(value), META_TTL)
        return value

    async def file_index(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        params = {"depth": "2"}
        return await self._cached(
            "index", key, params, lambda: self._get(f"/files/{quote(key)}", params)
        )

    async def nodes(self, key: str, ids: list[str], depth: int) -> dict[str, Any]:
        key = _file_key(key)
        params = {"ids": ",".join(_ids(ids)), "depth": str(max(1, min(depth, 6)))}
        return await self._cached(
            "nodes", key, params, lambda: self._get(f"/files/{quote(key)}/nodes", params)
        )

    async def render(self, key: str, ids: list[str], fmt: str, scale: float) -> dict[str, str]:
        key = _file_key(key)
        if fmt not in ("png", "jpg", "svg"):
            raise FigmaError("invalid_args", "Renders are png, jpg or svg.")
        params = {"ids": ",".join(_ids(ids)), "format": fmt}
        if fmt == "svg":
            params |= {"svg_outline_text": "true", "svg_include_id": "false"}
        else:
            params["scale"] = f"{max(0.25, min(scale, 4.0)):g}"

        async def fetch() -> dict[str, str]:
            body = await self._get(f"/images/{quote(key)}", params)
            images = body.get("images") if isinstance(body.get("images"), dict) else {}
            return {str(k): str(v) for k, v in images.items() if isinstance(v, str) and v}

        return await self._cached("render", key, params, fetch)

    async def variables(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        return await self._cached(
            "vars", key, {}, lambda: self._get(f"/files/{quote(key)}/variables/local")
        )


# ------------------------------------------------------------ connection


async def _connection(workspace_id: str, user_id: str) -> Any:
    from pocketpaw_ee.integrations import vault

    rows = [c for c in await vault.list_connections(workspace_id) if c.provider == "figma"]
    if not rows:
        raise FigmaError(
            "not_connected",
            "Figma is not connected for this workspace. Connect Figma in Integrations, "
            "then paste the link again.",
        )
    rows.sort(key=lambda c: (c.status != "active", c.created_by != user_id))
    return rows[0]


async def source_for(workspace_id: str, user_id: str) -> tuple[RestFigmaSource, Any]:
    """The workspace's Figma source and its connection row (for meta reads)."""
    from pocketpaw_ee.integrations import vault

    conn = await _connection(workspace_id, user_id)
    if conn.status != "active":
        raise FigmaError("needs_reconnect", RECONNECT)

    async def get_token(rejected: str = "") -> str:
        try:
            return await vault.get_valid_token(conn, rejected=rejected)
        except vault.NotConnected:
            raise FigmaError("needs_reconnect", RECONNECT) from None
        except Exception:  # noqa: BLE001 - provider down: never leak why
            raise FigmaError("unavailable", "Figma could not be reached. Try again.") from None

    async def on_limits(seen: dict[str, str]) -> None:
        coll = type(conn).get_pymongo_collection()
        await coll.update_one(
            {"_id": conn.id, "workspace": workspace_id},
            {"$set": {f"meta.{k}": v for k, v in seen.items()}},
        )
        conn.meta = {**(conn.meta or {}), **seen}

    return RestFigmaSource(get_token, scope=str(conn.id), on_limits=on_limits), conn
