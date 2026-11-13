# ee/pocketpaw_ee/sites/figma/client.py: the Figma read facade for the builder agent.
#
# ``FigmaSource`` is the contract the agent tools call: file metadata, a page /
# top-frame index, depth-limited nodes, render and raster bytes and local variables.
# Today the only implementation is ``RestFigmaSource`` over ``api.figma.com/v1`` with
# the caller's ``figma`` vault grant; a client for Figma's remote MCP can replace it
# once Figma admits us to its MCP catalog, without changing the tools. Never spoof a
# catalog client name to get there sooner.
#
# Tier-1 reads (the index, ``/nodes``, ``/images``) are the scarce ones: a View or
# Collab seat or a Starter plan gets a handful a month. So an import spends about two:
# ONE ``/nodes`` call for every wanted frame at ``FETCH_DEPTH`` (inspect and tokens
# trim it locally), ONE batched PNG render of the frames and raster nodes at
# ``RENDER_SCALE`` (downscaled locally), an SVG call only when icons are asked for,
# and the index only for a link without a node-id. Photo fills come from the tier-2
# fills list. Every answer is kept in ``body_cache`` (private store, per workspace and
# file ``version``, 30 days), so an unchanged file costs no tier-1 read again.
#
# Invariants a reader must not break:
#   * requests go only to ``API``; the file key and node ids are validated before
#     they reach a path or a query (``urls.py`` and ``_ids``); bytes are downloaded
#     only from ``FIGMA_IMAGE_HOSTS`` (``sites_reference._fetch``);
#   * the token comes from ``vault.get_valid_token`` per request and is never in a
#     result, an error or a log line. A 401, or Figma's 403 "Invalid token", gets
#     one refresh through ``get_token(rejected=...)``, then counts as a reconnect;
#   * never the whole file: ``file_index`` is ``/files/:key?depth=2`` and ``nodes``
#     is ``/files/:key/nodes`` with a depth;
#   * every cached read first makes the CALLER's own ``/meta`` call (tier 3, 10 min
#     per connection), which proves access and names the version the cache is keyed
#     on. 403 / 404 there is ``not_shared`` and nothing cached is read;
#   * 429 honours ``Retry-After``: a short wait is retried once, a longer one sets a
#     cooldown per connection, and the error carries ``X-Figma-Upgrade-Link``
#     (figma.com only). Plan-tier and rate-limit-type headers go to ``on_limits``;
#     each tier-1 read goes to ``on_read`` (the month's count on the connection);
#   * ``source_for`` uses only the CALLER's own figma connection (``created_by``),
#     never another member's: a token reaches its owner's personal files.

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.sites.figma import body_cache
from pocketpaw_ee.sites.figma import outline as figma_outline

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
META_TTL = 600
#: A Retry-After at or under this is waited out once; longer sets a cooldown.
SHORT_WAIT_S = 5
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_MEMORY_ENTRIES = 256
MAX_IDS = 20
#: ``/nodes`` depth: deep enough for inspect (<= 4 below a frame) and tokens under a
#: page or section, and every answer carries the ``styles`` / ``components`` maps.
FETCH_DEPTH = 6
RENDER_SCALE = 2.0
#: Figma's tier-1 allowance a month on a View / Collab seat or the Starter plan, as
#: published when this was written. An estimate: Figma's own count is authoritative.
METERED_MONTHLY_READS = 6
_FRAME_TYPES = ("FRAME", "COMPONENT")
_ID_RE = re.compile(r"I?\d{1,10}:\d{1,10}(?:;\d{1,10}:\d{1,10}){0,8}")
_KEY_RE = re.compile(r"[A-Za-z0-9]{10,64}")
_REF_RE = re.compile(r"[0-9a-f]{16,64}")

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
    async def read_render(
        self, key: str, ids: list[str], fmt: str = "png", scale: float = 1.0, *, strict: bool = True
    ) -> dict[str, bytes]: ...
    async def read_raster(self, key: str, ids: list[str]) -> dict[str, bytes]: ...
    async def variables(self, key: str) -> dict[str, Any]: ...
    async def reads_used(self, key: str) -> int: ...


# ---------------------------------------------------------------- small cache
# Meta answers, cooldowns and per-import counters only: bodies live in body_cache.

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
    """``FigmaSource`` over the REST API with one member's grant."""

    def __init__(
        self,
        get_token: Callable[..., Awaitable[str]],
        *,
        scope: str,
        workspace: str = "",
        on_limits: Callable[[dict[str, str]], Awaitable[None]] | None = None,
        on_read: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._get_token = get_token
        self._scope = scope
        self._workspace = workspace
        self._on_limits = on_limits
        self._on_read = on_read
        #: Tier-1 calls this source made.
        self.reads = 0
        self._versions: dict[str, str] = {}
        self._stores: dict[tuple[str, str], body_cache.Bodies | None] = {}
        self._memo: dict[tuple[str, str, str], Any] = {}
        self._trees: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    # -- the wire
    async def _get(
        self, path: str, params: dict[str, str] | None = None, *, tier1: str = ""
    ) -> dict[str, Any]:
        """One GET. ``tier1`` names the file key when this is a tier-1 read."""
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
        if tier1:
            await self._counted(tier1)
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

    # -- tier-1 accounting
    async def _counted(self, key: str) -> None:
        """One tier-1 read reached Figma: count it for this source, the file version
        (the import) and the month (``on_read``)."""
        self.reads += 1
        name = self._import_counter(key)
        try:
            held = int(await _cache_get(name) or 0)
        except ValueError:
            held = 0
        await _cache_set(name, str(held + 1), body_cache.TTL_SECONDS)
        if self._on_read is not None:
            try:
                await self._on_read(1)
            except Exception:  # noqa: BLE001 - a missed count never fails a read
                logger.debug("figma: read counter failed", exc_info=True)

    def _import_counter(self, key: str) -> str:
        return f"figma:imp:{self._scope}:{key}:{self._versions.get(key, '')}"

    async def reads_used(self, key: str) -> int:
        """Tier-1 reads this connection spent on this file version so far."""
        try:
            return int(await _cache_get(self._import_counter(_file_key(key))) or 0)
        except ValueError:
            return 0

    # -- the version gate and the body cache
    async def _version(self, key: str) -> str:
        """The caller's OWN ``/meta`` answer (tier 3). Every cached read waits on it,
        so a member who cannot open the file never reaches another member's cache."""
        return str((await self.file_meta(key)).get("version") or "")

    def _bodies(self, key: str, version: str) -> body_cache.Bodies | None:
        if not version or not self._workspace:
            return None
        slot = (key, version)
        if slot not in self._stores:
            self._stores[slot] = body_cache.for_version(self._workspace, key, version)
        return self._stores[slot]

    async def _json(self, key: str, version: str, name: str) -> Any:
        slot = (key, version, name)
        if slot in self._memo:
            return self._memo[slot]
        bodies = self._bodies(key, version)
        value = await bodies.get_json(name) if bodies else None
        if value is not None:
            self._memo[slot] = value
        return value

    async def _keep_json(self, key: str, version: str, name: str, value: Any) -> None:
        self._memo[(key, version, name)] = value
        bodies = self._bodies(key, version)
        if bodies:
            await bodies.put_json(name, value)

    async def _blob(self, key: str, version: str, name: str) -> bytes | None:
        slot = (key, version, name)
        if slot in self._memo:
            return self._memo[slot]
        bodies = self._bodies(key, version)
        data = await bodies.get(name) if bodies else None
        if data:
            self._memo[slot] = data
        return data

    async def _keep_blob(self, key: str, version: str, name: str, data: bytes) -> None:
        self._memo[(key, version, name)] = data
        bodies = self._bodies(key, version)
        if bodies:
            await bodies.put(name, data)

    # -- node trees
    async def _roots(self, key: str, version: str) -> dict[str, dict[str, Any]]:
        """Every node tree read for this file version: root id -> record."""
        slot = (key, version)
        if slot in self._trees:
            return self._trees[slot]
        roots: dict[str, dict[str, Any]] = {}
        bodies = self._bodies(key, version)
        for name in await bodies.names("nodes~") if bodies else []:
            rec = await self._json(key, version, name)
            if isinstance(rec, dict) and isinstance(rec.get("entry"), dict):
                roots[str(rec.get("id") or "")] = rec
        self._trees[slot] = roots
        return roots

    async def _plan(self, key: str, version: str) -> list[str]:
        """The frames an import wants: the index's site pages, plus each read tree's
        frame, or a page / section tree's child frames. Batched into one call."""
        out: list[str] = []
        index = await self._json(key, version, "index.json")
        if isinstance(index, dict):
            for page in figma_outline.site_pages(figma_outline.frames(index)):
                out += [str(v.get("node") or "") for v in page.get("viewports") or []]
        for rec in (await self._roots(key, version)).values():
            doc = rec["entry"].get("document") or {}
            if doc.get("type") in _FRAME_TYPES:
                out.append(str(doc.get("id") or ""))
            if doc.get("type") in figma_outline.CONTAINER_TYPES:
                out += [
                    str(c.get("id") or "")
                    for c in doc.get("children") or []
                    if isinstance(c, dict) and c.get("type") in _FRAME_TYPES
                ]
        return [i for i in dict.fromkeys(out) if _ID_RE.fullmatch(i)]

    async def _fetch_nodes(self, key: str, version: str, missing: list[str]) -> None:
        roots = await self._roots(key, version)
        extra = [i for i in await self._plan(key, version) if _find(roots, i, 1) is None]
        ask = list(dict.fromkeys(missing + extra))[:MAX_IDS]
        path = f"/files/{quote(key)}/nodes"
        try:
            body = await self._get(path, _nodes_params(ask), tier1=key)
        except FigmaError as exc:
            if exc.code != "too_large" or len(ask) == len(missing):
                raise
            ask = missing  # the batch was too big: just what was asked for
            body = await self._get(path, _nodes_params(ask), tier1=key)
        nodes = body.get("nodes") if isinstance(body.get("nodes"), dict) else {}
        for node_id, entry in nodes.items():
            if node_id not in ask or not isinstance(entry, dict):
                continue
            if not isinstance(entry.get("document"), dict):
                continue
            rec = {
                "id": node_id,
                "depth": FETCH_DEPTH,
                "name": str(body.get("name") or ""),
                "lastModified": str(body.get("lastModified") or ""),
                "entry": entry,
            }
            roots[node_id] = rec
            await self._keep_json(key, version, f"nodes~{body_cache.safe_id(node_id)}.json", rec)

    # -- the contract
    async def file_meta(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        name = f"figma:m:{self._scope}:{key}"
        hit = await _cache_get(name)
        if hit is not None:
            value = json.loads(hit)
        else:
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
        self._versions[key] = str(value.get("version") or "")
        return value

    async def file_index(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        version = await self._version(key)
        hit = await self._json(key, version, "index.json")
        if isinstance(hit, dict):
            return hit
        body = await self._get(f"/files/{quote(key)}", {"depth": "2"}, tier1=key)
        await self._keep_json(key, version, "index.json", body)
        return body

    async def nodes(self, key: str, ids: list[str], depth: int) -> dict[str, Any]:
        """``/nodes``'s shape for ``ids`` at ``depth``, trimmed from read trees.

        A miss reads every wanted frame at ``FETCH_DEPTH`` in ONE call, so inspect,
        tokens and the other viewports are served from it."""
        key = _file_key(key)
        ids = _ids(ids)
        depth = max(1, min(depth, FETCH_DEPTH))
        version = await self._version(key)
        roots = await self._roots(key, version)
        missing = [i for i in ids if _find(roots, i, depth) is None]
        if missing:
            await self._fetch_nodes(key, version, missing)
        out: dict[str, Any] = {}
        name = modified = ""
        for node_id in ids:
            hit = _find(roots, node_id, depth)
            if hit is None:
                out[node_id] = None
                continue
            rec, doc = hit
            name = rec.get("name") or name
            modified = rec.get("lastModified") or modified
            entry = {k: v for k, v in rec["entry"].items() if k != "document"}
            out[node_id] = {**entry, "document": _trim(doc, depth)}
        return {"name": name, "lastModified": modified, "nodes": out}

    async def _render_urls(
        self, key: str, ids: list[str], fmt: str, scale: float = RENDER_SCALE
    ) -> dict[str, str]:
        params = {"ids": ",".join(ids), "format": fmt}
        if fmt == "svg":
            params |= {"svg_outline_text": "true", "svg_include_id": "false"}
        else:
            params["scale"] = f"{scale:g}"
        body = await self._get(f"/images/{quote(key)}", params, tier1=key)
        images = body.get("images") if isinstance(body.get("images"), dict) else {}
        return {str(k): str(v) for k, v in images.items() if isinstance(v, str) and v}

    async def _rendered(
        self, key: str, version: str, ids: list[str], fmt: str, scale: float, wanted: list[str]
    ) -> tuple[dict[str, bytes], FigmaError | None]:
        """One ``/images`` call, every answer downloaded and kept."""
        urls = await self._render_urls(key, ids, fmt, scale)
        got, failure = await _download_all(urls, ids, wanted)
        for node_id, data in got.items():
            await self._keep_blob(key, version, _render_name(fmt, node_id, scale), data)
        return got, failure

    async def read_render(
        self, key: str, ids: list[str], fmt: str = "png", scale: float = 1.0, *, strict: bool = True
    ) -> dict[str, bytes]:
        """Render bytes by node id, downloaded as soon as Figma answers (its URLs
        expire). PNG renders at ``RENDER_SCALE`` in ONE call for the asked ids plus
        every wanted frame and raster node, and is downscaled locally to ``scale``;
        an asked frame too big for that is rendered once more at 1x. SVG is one call
        for the asked ids. ``strict`` raises when an asked id could not be fetched
        from Figma's image hosts and nothing asked for came back."""
        key = _file_key(key)
        ids = _ids(ids)
        if fmt not in ("png", "svg"):
            raise FigmaError("invalid_args", "Renders are png or svg.")
        version = await self._version(key)
        full: dict[str, bytes] = {}  # at RENDER_SCALE (svg: as is)
        plain: dict[str, bytes] = {}  # png at 1x, only when 2x failed
        for node_id in ids:
            data = await self._blob(key, version, _render_name(fmt, node_id))
            if data:
                full[node_id] = data
            elif fmt == "png" and (
                data := await self._blob(key, version, _render_name(fmt, node_id, 1.0))
            ):
                plain[node_id] = data
        missing = [i for i in ids if i not in full and i not in plain]
        failure: FigmaError | None = None
        if missing:
            ask = list(missing)
            if fmt == "png":
                roots = await self._roots(key, version)
                extra = [*await self._plan(key, version), *_raster_nodes(roots)]
                for node_id in dict.fromkeys(extra):
                    if len(ask) >= MAX_IDS:
                        break
                    if node_id in ask:
                        continue
                    if not await self._blob(key, version, _render_name(fmt, node_id)):
                        ask.append(node_id)
            got, failure = await self._rendered(key, version, ask, fmt, RENDER_SCALE, missing)
            full |= {i: d for i, d in got.items() if i in missing}
            again = [i for i in missing if i not in full]
            refused = failure is not None and failure.code == "render_refused"
            if fmt == "png" and again and not refused:
                # Past Figma's render or our download cap at 2x: 1x for just these.
                got, late = await self._rendered(key, version, again, fmt, 1.0, again)
                plain |= got
                failure = late or failure
        if strict and failure is not None and not any(i in full or i in plain for i in missing):
            raise failure
        if fmt == "png" and scale < RENDER_SCALE:
            factor = max(0.125, scale / RENDER_SCALE)
            full = {i: await asyncio.to_thread(_downscale, d, factor) for i, d in full.items()}
        return {**plain, **full}

    async def image_fills(self, key: str) -> dict[str, str]:
        """``imageRef`` -> download URL for the file's image fills (tier 2)."""
        key = _file_key(key)
        body = await self._get(f"/files/{quote(key)}/images")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        images = meta.get("images") if isinstance(meta.get("images"), dict) else {}
        return {str(k): str(v) for k, v in images.items() if isinstance(v, str) and v}

    async def read_raster(self, key: str, ids: list[str]) -> dict[str, bytes]:
        """Raster bytes by node id: a photo fill's original image from the tier-2
        fills list when the read tree names one, else the batched PNG render."""
        key = _file_key(key)
        ids = _ids(ids)
        version = await self._version(key)
        roots = await self._roots(key, version)
        out: dict[str, bytes] = {}
        pending: dict[str, str] = {}
        for node_id in ids:
            hit = _find(roots, node_id, 0)
            ref = _image_ref(hit[1]) if hit else ""
            if not ref:
                continue
            data = await self._blob(key, version, f"fill~{ref}.bin")
            if data:
                out[node_id] = data
            else:
                pending[node_id] = ref
        if pending:
            try:
                fills = await self.image_fills(key)
            except FigmaError as exc:
                if exc.code not in ("not_shared", "unavailable", "too_large"):
                    raise
                fills = {}
            got, _ = await _download_all(
                {i: fills.get(r, "") for i, r in pending.items()}, list(pending), []
            )
            for node_id, data in got.items():
                await self._keep_blob(key, version, f"fill~{pending[node_id]}.bin", data)
                out[node_id] = data
        rest = [i for i in ids if i not in out]
        if rest:
            out |= await self.read_render(key, rest, "png", RENDER_SCALE, strict=False)
        return out

    async def variables(self, key: str) -> dict[str, Any]:
        key = _file_key(key)
        version = await self._version(key)
        hit = await self._json(key, version, "variables.json")
        if isinstance(hit, dict):
            return hit
        body = await self._get(f"/files/{quote(key)}/variables/local")
        await self._keep_json(key, version, "variables.json", body)
        return body


# ---------------------------------------------------------------- helpers


def _nodes_params(ids: list[str]) -> dict[str, str]:
    return {"ids": ",".join(ids), "depth": str(FETCH_DEPTH)}


def _render_name(fmt: str, node_id: str, scale: float = RENDER_SCALE) -> str:
    kind = fmt if fmt == "svg" or scale == RENDER_SCALE else f"{fmt}1x"
    return f"render-{kind}~{body_cache.safe_id(node_id)}.bin"


def _find(
    roots: dict[str, dict[str, Any]], node_id: str, depth: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(record, node)`` when a read tree holds ``node_id`` with ``depth`` levels
    below it still inside what was fetched."""
    rec = roots.get(node_id)
    if rec is not None:
        return rec, rec["entry"]["document"]
    for rec in roots.values():
        room = int(rec.get("depth") or 0)
        stack = [(rec["entry"].get("document") or {}, 0)]
        while stack:
            node, level = stack.pop()
            if node.get("id") == node_id:
                if room - level >= depth:
                    return rec, node
                break
            if level < room:
                kids = node.get("children") or []
                stack += [(c, level + 1) for c in kids if isinstance(c, dict)]
    return None


def _trim(node: dict[str, Any], depth: int) -> dict[str, Any]:
    out = {k: v for k, v in node.items() if k != "children"}
    kids = node.get("children")
    if depth > 0 and isinstance(kids, list):
        out["children"] = [_trim(c, depth - 1) for c in kids if isinstance(c, dict)]
    return out


def _image_fill(node: dict[str, Any]) -> dict[str, Any] | None:
    for paint in node.get("fills") or []:
        if not isinstance(paint, dict) or paint.get("visible") is False:
            continue
        if paint.get("type") == "IMAGE":
            return paint
    return None


def _image_ref(node: dict[str, Any]) -> str:
    ref = str((_image_fill(node) or {}).get("imageRef") or "")
    return ref if _REF_RE.fullmatch(ref) else ""


def _raster_nodes(roots: dict[str, dict[str, Any]]) -> list[str]:
    """Image-filled nodes with no usable ``imageRef``: only a render reaches them."""
    out: list[str] = []
    for rec in roots.values():
        stack = [rec["entry"].get("document") or {}]
        while stack:
            node = stack.pop()
            if _image_fill(node) is not None and not _image_ref(node):
                out.append(str(node.get("id") or ""))
            stack += [c for c in node.get("children") or [] if isinstance(c, dict)]
    return [i for i in out if _ID_RE.fullmatch(i)]


async def _download(url: str) -> bytes:
    """Bytes from one of Figma's image hosts (exact match, streamed, capped)."""
    from pocketpaw_ee.agent.mcp_servers.sites_reference import _fetch, _fetch_allowed
    from pocketpaw_ee.sites.site_references import SiteReferenceError

    if not _fetch_allowed(url):
        raise FigmaError("render_refused", "the render came from an unknown host.")
    try:
        return await _fetch(url)
    except SiteReferenceError as exc:
        raise FigmaError("render_unreadable", str(exc)) from None


async def _download_all(
    urls: dict[str, str], ask: list[str], wanted: list[str]
) -> tuple[dict[str, bytes], FigmaError | None]:
    """Download every URL Figma gave (four at a time). The first failure on a
    ``wanted`` id is returned so a caller can say why its own id came back empty."""
    gate = asyncio.Semaphore(4)
    failures: list[FigmaError] = []

    async def one(node_id: str) -> tuple[str, bytes | None]:
        url = urls.get(node_id, "")
        if not url:
            return node_id, None
        try:
            async with gate:
                return node_id, await _download(url)
        except FigmaError as exc:
            if node_id in wanted:
                failures.append(exc)
        except Exception:  # noqa: BLE001
            logger.debug("figma: download failed", exc_info=True)
            if node_id in wanted:
                failures.append(FigmaError("render_unreadable", "the render could not be read."))
        return node_id, None

    pairs = await asyncio.gather(*(one(i) for i in ask))
    return {i: d for i, d in pairs if d}, (failures[0] if failures else None)


def _downscale(data: bytes, factor: float) -> bytes:
    """A PNG at ``factor`` of its size; the bytes unchanged when they won't decode."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 60_000_000:
                return data
            size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
            small = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            small.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return data


# ---------------------------------------------------------------- the month


def month_now() -> str:
    """The UTC calendar month tier-1 reads are counted in, ``YYYY-MM``."""
    return time.strftime("%Y-%m", time.gmtime())


def monthly_reads(meta: dict[str, Any] | None) -> int:
    """Tier-1 reads a connection's meta records for the current month."""
    meta = meta or {}
    if meta.get("tier1_month") != month_now():
        return 0
    try:
        return int(meta.get("tier1_reads") or 0)
    except (TypeError, ValueError):
        return 0


def count_read(meta: dict[str, Any] | None, n: int = 1) -> dict[str, Any]:
    """``meta`` with ``n`` more tier-1 reads this month (a new month starts at 0)."""
    return {**(meta or {}), "tier1_month": month_now(), "tier1_reads": monthly_reads(meta) + n}


# ------------------------------------------------------------ connection


async def _connection(workspace_id: str, user_id: str) -> Any:
    from pocketpaw_ee.integrations import vault

    # The caller's OWN grant only: a member's token reaches their personal files, so
    # another member must never read through it.
    rows = [
        c
        for c in await vault.list_connections(workspace_id)
        if c.provider == "figma" and user_id and c.created_by == user_id
    ]
    if not rows:
        raise FigmaError(
            "not_connected",
            "Connect your Figma account in Integrations (each member connects their own), "
            "then paste the link again.",
        )
    rows.sort(key=lambda c: c.status != "active")
    return rows[0]


async def source_for(workspace_id: str, user_id: str) -> tuple[RestFigmaSource, Any]:
    """The caller's own Figma source and its connection row (for meta reads)."""
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

    def coll() -> Any:
        return type(conn).get_pymongo_collection()

    async def on_limits(seen: dict[str, str]) -> None:
        await coll().update_one(
            {"_id": conn.id, "workspace": workspace_id},
            {"$set": {f"meta.{k}": v for k, v in seen.items()}},
        )
        conn.meta = {**(conn.meta or {}), **seen}

    async def on_read(n: int) -> None:
        month = month_now()
        done = await coll().update_one(
            {"_id": conn.id, "workspace": workspace_id, "meta.tier1_month": month},
            {"$inc": {"meta.tier1_reads": n}},
        )
        if not getattr(done, "matched_count", 0):
            # A new month (or the first read): the count starts over.
            await coll().update_one(
                {"_id": conn.id, "workspace": workspace_id},
                {"$set": {"meta.tier1_month": month, "meta.tier1_reads": n}},
            )
        conn.meta = count_read(conn.meta, n)

    source = RestFigmaSource(
        get_token,
        scope=str(conn.id),
        workspace=workspace_id,
        on_limits=on_limits,
        on_read=on_read,
    )
    return source, conn
